r"""Optional Chrome/CDP browser smoke for the frontend connection.

Prerequisites (run separately):
  1. Core API on --core-endpoint with a real principal credential
  2. frontend/dev_server.py on --frontend-port
  3. Chrome headless with --remote-debugging-port=9222 pointing at the page

Example:
  chrome --headless=new --remote-debugging-port=9222 ^
    --remote-allow-origins=* --user-data-dir=%TEMP%\cc-profile ^
    http://127.0.0.1:4174/
  python scripts/frontend_browser_smoke.py --api-key <secret>

Requires the optional dev dependency `websocket-client` (requirements-dev.txt).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--core-endpoint", default="http://127.0.0.1:8876")
    parser.add_argument("--project", default="proj_lab")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--upload", action="store_true", help="also test a text file upload")
    parser.add_argument("--upload-image", action="store_true", help="use a JPEG instead of text for upload")
    parser.add_argument(
        "--model-key", default=os.environ.get("CC_TEST_MODEL_KEY", ""),
        help="optional real model key; configures glm-5.3-flash through the settings UI",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/repair/screenshots/frontend-connected.png"),
    )
    args = parser.parse_args()

    deadline = time.time() + 20
    page = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{args.cdp}/json", timeout=5) as response:
                targets = json.loads(response.read())
            page = next(t for t in targets if t.get("type") == "page")
            break
        except Exception:
            time.sleep(0.5)
    if page is None:
        raise SystemExit("No Chrome CDP page target. Start Chrome with --remote-debugging-port.")
    ws = websocket.create_connection(
        page["webSocketDebuggerUrl"], timeout=15, suppress_origin=True
    )
    next_id = 0

    def command(method: str, params: dict | None = None) -> dict:
        nonlocal next_id
        next_id += 1
        ws.send(json.dumps({"id": next_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(ws.recv())
            if message.get("id") == next_id:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def evaluate(expression: str):
        return command(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        ).get("result", {}).get("value")

    command("Page.enable")
    command("Runtime.enable")
    time.sleep(2)
    evaluate("document.querySelector('[data-action=\"settings\"]').click()")
    time.sleep(0.5)
    evaluate(
        """
        (() => {
          const form = document.querySelector('form[data-form="connect"]');
          form.querySelector('input[name="endpoint"]').value = %s;
          form.querySelector('input[name="projectId"]').value = %s;
          form.querySelector('input[name="apiKey"]').value = %s;
          form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
          return true;
        })()
        """
        % (
            json.dumps(args.core_endpoint),
            json.dumps(args.project),
            json.dumps(args.api_key),
        )
    )
    time.sleep(3)
    text = evaluate("document.body.innerText") or ""
    if "CORE · 只读" not in text and "已连接" not in text:
        raise SystemExit(f"Connection failed; page text: {text[:800]}")
    if not evaluate("!!document.querySelector('#task-composer')"):
        raise SystemExit("Unified task composer did not render on the homepage.")

    model_tested = False
    if args.model_key:
        evaluate("document.querySelector('[data-action=\"settings\"]').click()")
        time.sleep(0.5)
        if not evaluate("!!document.querySelector('[data-action=\"model-config\"]')"):
            raise SystemExit("Model config entry did not render in settings.")
        evaluate("document.querySelector('[data-action=\"model-config\"]').click()")
        time.sleep(0.5)
        if not evaluate("!!document.querySelector('form[data-form=\"model\"]')"):
            raise SystemExit("Model config form did not render.")
        evaluate(
            """
            (() => {
              const form = document.querySelector('form[data-form="model"]');
              form.querySelector('input[name="baseUrl"]').value = 'https://open.bigmodel.cn/api/paas/v4';
              form.querySelector('input[name="model"]').value = 'glm-5.3-flash';
              form.querySelector('input[name="apiKey"]').value = %s;
              form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
              return true;
            })()
            """ % json.dumps(args.model_key)
        )
        time.sleep(10)
        model_text = evaluate("document.body.innerText") or ""
        if "LLM · glm-5.3-flash" not in model_text:
            raise SystemExit(f"Model settings did not activate: {model_text[:1200]}")
        model_tested = True

    # Submit a natural-language query through the unified task box.
    evaluate("""
    (() => {
      const input = document.querySelector('#task-text');
      const form = document.querySelector('form[data-form="task"]');
      input.value = 'SAMPLE-002 实际取用多少？';
      form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      return true;
    })()
    """)
    time.sleep(25 if model_tested else 5)
    text = evaluate("document.body.innerText") or ""
    if "b_query" not in text and "query_answer" not in text:
        raise SystemExit(f"Task result did not appear; page text: {text[:1600]}")
    if "8" not in text:
        raise SystemExit(f"Task result did not contain the expected quantity; page text: {text[:1600]}")
    if model_tested and "\nLLM\n" not in text and "LLM" not in text:
        raise SystemExit(f"Task did not use the configured model: {text[:1600]}")

    upload_ok = False
    if args.upload or args.upload_image:
        if args.upload_image:
            upload_path = Path(r"D:\Chem\ChemClaw-Lab-S10-Repair\frontend\docs\screenshots\overview-desktop.jpg")
            upload_text = "整理这张实验照片"
        else:
            upload_path = Path(tempfile.gettempdir()) / "chemclaw-ui-upload.txt"
            upload_path.write_text("实际取用 0.5 g SAMPLE-002", encoding="utf-8")
            upload_text = "整理这份实验记录"
        command("DOM.enable")
        document = command("DOM.getDocument", {"depth": -1})
        node = command("DOM.querySelector", {
            "nodeId": document["root"]["nodeId"],
            "selector": "#task-file",
        })
        if not node.get("nodeId"):
            raise SystemExit("File input was not found.")
        command("DOM.setFileInputFiles", {
            "files": [str(upload_path)],
            "nodeId": node["nodeId"],
        })
        evaluate("document.querySelector('#task-file').dispatchEvent(new Event('change', {bubbles: true}))")
        time.sleep(2)
        files_text = evaluate("document.querySelector('#task-files').innerText") or ""
        if upload_path.name not in files_text:
            raise SystemExit(f"File chip did not render: {files_text!r}")
        evaluate("""
        (() => {
          const input = document.querySelector('#task-text');
          const form = document.querySelector('form[data-form="task"]');
          input.value = %s;
          form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
          return true;
        })()
        """ % json.dumps(upload_text, ensure_ascii=False))
        if args.upload_image and model_tested:
            deadline = time.time() + 240
            upload_text = evaluate("document.body.innerText") or ""
            while "处理中…" in upload_text and time.time() < deadline:
                time.sleep(4)
                upload_text = evaluate("document.body.innerText") or ""
        else:
            time.sleep(25 if model_tested else 7)
            upload_text = evaluate("document.body.innerText") or ""
        if "b_capture" not in upload_text:
            raise SystemExit(f"Upload task did not reach B: {upload_text[:1600]}")
        if "提案" not in upload_text and "原件已保存" not in upload_text:
            raise SystemExit(f"Upload result missing proposal/original status: {upload_text[:1600]}")
        upload_ok = True

    evaluate("document.querySelector('#task-composer')?.scrollIntoView({block:'start'})")
    time.sleep(0.5)
    shot = command("Page.captureScreenshot", {"format": "png"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(base64.b64decode(shot["data"]))

    evaluate("location.hash = '#inventory'")
    time.sleep(2)
    text = evaluate("document.body.innerText") or ""
    if "实验室共享" not in text:
        raise SystemExit(f"Inventory view did not render; page text: {text[:1200]}")
    print(json.dumps({
        "status": "OK",
        "title": evaluate("document.title"),
        "connected": True,
        "taskComposer": True,
        "modelSettingsTested": model_tested,
        "taskRouted": True,
        "uploadTested": upload_ok,
        "inventoryRendered": True,
        "screenshot": str(args.output),
    }, ensure_ascii=False))
    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
