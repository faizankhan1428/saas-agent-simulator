import logging
import os
import re
import shutil
import time
import zipfile

from flask import Flask, jsonify, render_template, request, send_from_directory
from groq import Groq

app = Flask(__name__)
logger = logging.getLogger(__name__)

MODEL = "llama-3.3-70b-versatile"

# Vercel Serverless Writable Writable Directory Fix (/tmp)
GENERATED_DIR = "/tmp/generated_project"
STATIC_DIR = "/tmp/static"
PROJECT_ZIP_NAME = "project.zip"

FALLBACK_README = "readme_output.txt"
AGENT_UNAVAILABLE_MSG = "AI Agents are temporarily unavailable. Please try again."
EXPECTED_PIPELINE_STEPS = 4

# Matches blocks like:
#   ---FILE: path/to/file.ext---
#   <code>
#   ---END_FILE---
FILE_BLOCK_PATTERN = re.compile(
    r"---FILE:\s*(.+?)\s*---\s*\n(.*?)\s*---END_FILE---",
    re.DOTALL,
)

FILE_FORMAT_INSTRUCTION = (
    "You MUST output every project file using this exact delimiter format "
    "(do NOT wrap files in markdown code fences):\n\n"
    "---FILE: path/to/filename.ext---\n"
    "[complete file contents here]\n"
    "---END_FILE---\n\n"
    "Include all necessary source files, configuration files, and a README. "
    "Use forward slashes in file paths. Each file must have both delimiters."
)

# ── Agent personas (system prompts) ───────────────────────────────────────────
AGENT_PROMPTS = {
    "product_manager": (
        "You are an expert SaaS Product Manager. "
        "Define core features and requirements."
    ),
    "lead_developer": (
        "You are an expert Full-Stack Developer. "
        "Plan the file structure and write clean code architectures."
    ),
    "qa_engineer": (
        "You are a strict QA Engineer. "
        "Find edge cases, security flaws, and improvements in the developer's plan."
    ),
}

AGENT_LABELS = {
    "product_manager": "Product Manager",
    "lead_developer": "Lead Developer",
    "qa_engineer": "QA Engineer",
}

def _get_client():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY environment variable is not set")
    return Groq(api_key=api_key)


def cleanup_previous_generation() -> list[str]:
    """
    Remove artifacts from a prior generation run before starting a new one.
    Safely deletes generated_project/ and static/project.zip if they exist inside /tmp.
    """
    warnings: list[str] = []

    if os.path.isdir(GENERATED_DIR):
        try:
            shutil.rmtree(GENERATED_DIR)
        except OSError as exc:
            warnings.append(f"Could not fully remove {GENERATED_DIR}: {exc}")

    zip_path = os.path.join(STATIC_DIR, PROJECT_ZIP_NAME)
    if os.path.isfile(zip_path):
        try:
            os.remove(zip_path)
        except OSError as exc:
            warnings.append(f"Could not remove {zip_path}: {exc}")

    return warnings


def call_agent(system_prompt: str, user_message: str, agent_key: str) -> str:
    """Run a single agent turn via the Groq chat completions API."""
    # time.sleep(1) -> Disabled or kept minimal to prevent Vercel 10s timeouts
    time.sleep(0.2)

    client = _get_client()
    agent_name = AGENT_LABELS.get(agent_key, agent_key)

    try:
        completion = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
    except Exception as exc:
        logger.error("Groq API error for %s: %s", agent_name, exc)
        raise

    text = (completion.choices[0].message.content or "").strip()
    if not text:
        raise ValueError(f"{agent_name} returned an empty response.")
    return text


def _safe_relative_path(filepath: str) -> str | None:
    """
    Normalize and validate a model-supplied path so it cannot escape
    the generated_project root (blocks '..', absolute paths, drive letters).
    """
    cleaned = filepath.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        return None

    normalized = os.path.normpath(cleaned)
    if normalized.startswith("..") or os.path.isabs(normalized):
        return None

    return normalized.replace("\\", "/")


def _write_text_file(relative_path: str, content: str) -> None:
    """Write a UTF-8 text file under generated_project/."""
    full_path = os.path.join(GENERATED_DIR, relative_path.replace("/", os.sep))
    parent_dir = os.path.dirname(full_path)
    os.makedirs(parent_dir, exist_ok=True)
    with open(full_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _save_fallback_readme(developer_output: str, result: dict) -> None:
    """
    Fallback when delimiter parsing fails: persist the full raw developer
    response so the user still receives something downloadable.
    """
    try:
        os.makedirs(GENERATED_DIR, exist_ok=True)
        _write_text_file(FALLBACK_README, developer_output)
        result["saved_files"].append(FALLBACK_README)
        result["warnings"].append(
            "Delimiter format not detected; saved full response as readme_output.txt."
        )
    except OSError as exc:
        result["warnings"].append(f"Fallback save to readme_output.txt failed: {exc}")


def save_generated_files(developer_output: str) -> dict:
    """
    Parse delimiter blocks from the final developer response and write files
    into generated_project/.
    """
    result: dict = {"saved_files": [], "warnings": [], "used_fallback": False}

    if not developer_output or not developer_output.strip():
        result["warnings"].append("Developer output was empty; no files written.")
        return result

    matches = re.findall(FILE_BLOCK_PATTERN, developer_output)
    if not matches:
        _save_fallback_readme(developer_output, result)
        result["used_fallback"] = True
        return result

    os.makedirs(GENERATED_DIR, exist_ok=True)

    for raw_path, raw_content in matches:
        safe_path = _safe_relative_path(raw_path)
        if not safe_path:
            result["warnings"].append(f"Skipped unsafe file path: {raw_path!r}")
            continue

        content = raw_content.strip("\n")

        try:
            _write_text_file(safe_path, content)
            result["saved_files"].append(safe_path)
        except OSError as exc:
            result["warnings"].append(f"Failed to write {safe_path}: {exc}")

    if not result["saved_files"]:
        result["warnings"].append(
            "No valid delimited files could be saved; using raw-response fallback."
        )
        _save_fallback_readme(developer_output, result)
        result["used_fallback"] = True

    return result


def zip_directory(directory_path: str, zip_name: str) -> str:
    """Compress every file under directory_path into static/<zip_name> inside /tmp."""
    if not os.path.isdir(directory_path):
        raise FileNotFoundError(f"Directory not found: {directory_path}")

    os.makedirs(STATIC_DIR, exist_ok=True)
    zip_path = os.path.join(STATIC_DIR, zip_name)

    files_written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, _, filenames in os.walk(directory_path):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                arcname = os.path.relpath(file_path, directory_path)
                archive.write(file_path, arcname)
                files_written += 1

    if files_written == 0:
        raise ValueError(f"No files found in {directory_path} to zip.")

    return os.path.abspath(zip_path)


def _timeline_entry(step: int, agent_key: str, user_input: str, agent_output: str) -> dict:
    """Build one chat-timeline record for the JSON response."""
    return {
        "step": step,
        "agent": AGENT_LABELS[agent_key],
        "agent_key": agent_key,
        "input": user_input,
        "output": agent_output,
    }


def _validate_timeline(timeline: list[dict]) -> None:
    """Ensure every pipeline step returned non-empty output before packaging."""
    if len(timeline) != EXPECTED_PIPELINE_STEPS:
        raise ValueError(
            f"Expected {EXPECTED_PIPELINE_STEPS} agent steps, got {len(timeline)}."
        )

    for entry in timeline:
        output = (entry.get("output") or "").strip()
        if not output:
            agent_name = entry.get("agent", "Unknown agent")
            raise ValueError(f"{agent_name} produced no output.")


def _validate_success_payload(
    user_prompt: str,
    timeline: list[dict],
    final_deliverable: str,
    saved_files: list[str],
    download_url: str | None,
) -> dict:
    """Assemble and sanity-check the final JSON payload returned to the client."""
    if not user_prompt:
        raise ValueError("user_prompt is missing from the success payload.")
    if not final_deliverable or not final_deliverable.strip():
        raise ValueError("final_deliverable is empty.")

    _validate_timeline(timeline)

    payload = {
        "success": True,
        "user_prompt": user_prompt,
        "timeline": timeline,
        "final_deliverable": final_deliverable,
        "saved_files": saved_files,
        "download_url": download_url,
    }

    return payload


def _agent_unavailable_response():
    """Return a safe client-facing message; caller should log the real error."""
    return jsonify({"success": False, "error": AGENT_UNAVAILABLE_MSG}), 500


@app.route("/")
def index():
    return render_template("index.html")


# New custom route to bypass Vercel read-only system and download zip safely from /tmp
@app.route("/download-zip")
def download_zip():
    return send_from_directory(STATIC_DIR, PROJECT_ZIP_NAME, as_attachment=True)


@app.route("/generate", methods=["POST"])
def generate():
    """
    Orchestrate a 4-step sequential multi-agent pipeline and return a chat timeline.
    """
    data = request.get_json(silent=True) or {}
    user_prompt = (data.get("user_prompt") or "").strip()

    if not user_prompt:
        return jsonify({"success": False, "error": "user_prompt is required"}), 400

    if len(user_prompt) > 8000:
        return jsonify({
            "success": False,
            "error": "user_prompt is too long (max 8000 characters).",
        }), 400

    cleanup_warnings = cleanup_previous_generation()

    timeline: list[dict] = []
    final_plan = ""

    # ── Multi-agent loop (Groq calls) ───────────────────────────────────────
    try:
        pm_input = (
            "The user wants to build the following Micro-SaaS product:\n\n"
            f"{user_prompt}\n\n"
            "Define the core features, user personas, and functional requirements."
        )
        pm_insights = call_agent(AGENT_PROMPTS["product_manager"], pm_input, "product_manager")
        timeline.append(_timeline_entry(1, "product_manager", pm_input, pm_insights))

        dev_input = (
            "Build a technical plan for this Micro-SaaS product.\n\n"
            f"## Original Idea\n{user_prompt}\n\n"
            f"## Product Manager Requirements\n{pm_insights}\n\n"
            "Provide a file structure, technology choices, and clean architecture."
        )
        dev_plan = call_agent(AGENT_PROMPTS["lead_developer"], dev_input, "lead_developer")
        timeline.append(_timeline_entry(2, "lead_developer", dev_input, dev_plan))

        qa_input = (
            "Review the following developer architecture plan.\n\n"
            f"## Developer Plan\n{dev_plan}\n\n"
            "List edge cases, security flaws, and concrete improvements."
        )
        qa_feedback = call_agent(AGENT_PROMPTS["qa_engineer"], qa_input, "qa_engineer")
        timeline.append(_timeline_entry(3, "qa_engineer", qa_input, qa_feedback))

        revision_input = (
            "Revise your plan based on QA feedback and output a minimal, runnable project.\n\n"
            f"## Original Idea\n{user_prompt}\n\n"
            f"## Your Previous Plan\n{dev_plan}\n\n"
            f"## QA Feedback\n{qa_feedback}\n\n"
            "## Efficiency rules (MANDATORY)\n"
            "1. Do NOT write generic boilerplate, placeholder stubs, or repetitive comments.\n"
            "2. Focus ONLY on the core runnable components of this Micro-SaaS — "
            "output a maximum of 2-3 essential files (e.g., main app, one config/route file, "
            "and optionally a short README).\n"
            "3. Keep every file small and clean so generation completes in under 45 seconds. "
            "Prefer concise, production-quality code over exhaustive coverage.\n\n"
            "Address the most critical QA concerns only. Skip nice-to-haves, tests, and extra layers.\n\n"
            "Output the project files using the delimiter format below.\n\n"
            f"{FILE_FORMAT_INSTRUCTION}"
        )
        final_plan = call_agent(AGENT_PROMPTS["lead_developer"], revision_input, "lead_developer")
        timeline.append(_timeline_entry(4, "lead_developer", revision_input, final_plan))

        _validate_timeline(timeline)

    except ValueError as exc:
        message = str(exc)
        if "GROQ_API_KEY" in message or "empty response" in message.lower():
            logger.error("Agent configuration/response error: %s", message)
            return _agent_unavailable_response()
        return jsonify({"success": False, "error": message}), 500
    except Exception as exc:
        logger.exception("Groq API error during agent pipeline: %s", exc)
        return _agent_unavailable_response()

    # ── Post-processing: extract files and build downloadable zip ─────────────
    file_result: dict = {"saved_files": [], "warnings": list(cleanup_warnings), "used_fallback": False}
    download_url: str | None = None

    try:
        file_result = save_generated_files(final_plan)
        file_result["warnings"] = cleanup_warnings + file_result.get("warnings", [])

        if file_result["saved_files"]:
            zip_directory(GENERATED_DIR, PROJECT_ZIP_NAME)
            # Route updated to dynamic endpoint to serve file from secure /tmp storage
            download_url = "/download-zip"

    except (FileNotFoundError, ValueError, OSError, zipfile.BadZipFile) as exc:
        logger.exception("File packaging failed: %s", exc)
        file_result.setdefault("warnings", []).append(f"Packaging failed: {exc}")

    try:
        payload = _validate_success_payload(
            user_prompt=user_prompt,
            timeline=timeline,
            final_deliverable=final_plan,
            saved_files=file_result.get("saved_files", []),
            download_url=download_url,
        )
        packaging_warnings = list(file_result.get("warnings", []))
        if file_result.get("saved_files") and not download_url:
            packaging_warnings.append(
                "Files were saved but no download URL was produced."
            )
        payload["file_warnings"] = packaging_warnings
        payload["used_fallback"] = file_result.get("used_fallback", False)
        return jsonify(payload)

    except ValueError as exc:
        logger.exception("Response validation failed: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True)
