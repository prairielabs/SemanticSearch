#!/usr/bin/env python3
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_NAME = "Semantic Search"
DEFAULT_MODEL = "mistral-medium-3-5"
API_BASE = "https://api.mistral.ai"
PAYLOAD_DIR = Path(__file__).resolve().parent
BASE_HTML = PAYLOAD_DIR / "base.html"
INDEX_HTML = PAYLOAD_DIR / "index.html"
CONFIG_DIR = PAYLOAD_DIR / "config"
HISTORY_DIR = PAYLOAD_DIR / "history"
LOG_DIR = PAYLOAD_DIR / "logs"
LIVE_LOG = LOG_DIR / "live.log"
LANGUAGE_SCRIPT_ID = "semantic-search-language-contract"
AGENT_PROMPT_VERSION = "prairie-google-clickable-scores-2026-06-30-v2"
LIVE_LOG_LOCK = threading.Lock()


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def read_text(path, default=""):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def clean_config_text(value):
    return value.replace("\ufeff", "").strip()


def write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def live_log(message):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {message}\n"
    with LIVE_LOG_LOCK:
        with LIVE_LOG.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key_file = CONFIG_DIR / "mistral_api_key.txt"
    if not key_file.exists():
        write_text_atomic(key_file, "PASTE_MISTRAL_API_KEY_HERE\n")
    model_file = CONFIG_DIR / "mistral_model.txt"
    if not model_file.exists():
        write_text_atomic(model_file, DEFAULT_MODEL + "\n")
    if BASE_HTML.exists() and not INDEX_HTML.exists():
        shutil.copyfile(BASE_HTML, INDEX_HTML)


def log_error(label, exc):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    live_log(f"ERROR: {label} {exc!r}")
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"error_{stamp}.txt"
    body = [label, "", repr(exc), "", traceback.format_exc()]
    write_text_atomic(path, "\n".join(body))


def get_config_value(name, default=""):
    env = os.environ.get(name)
    if env:
        return clean_config_text(env)
    file_name = name.lower()
    if file_name.startswith("mistral_"):
        file_name = file_name + ".txt"
    value = clean_config_text(read_text(CONFIG_DIR / file_name, default))
    return value


def get_api_key():
    key = clean_config_text(os.environ.get("MISTRAL_API_KEY", ""))
    if key:
        return key
    key = clean_config_text(read_text(CONFIG_DIR / "mistral_api_key.txt"))
    if not key or key == "PASTE_MISTRAL_API_KEY_HERE":
        return ""
    return key


def get_model():
    return (
        clean_config_text(os.environ.get("MISTRAL_MODEL", ""))
        or clean_config_text(read_text(CONFIG_DIR / "mistral_model.txt", DEFAULT_MODEL))
        or DEFAULT_MODEL
    )


def get_cached_agent_id():
    env_agent_id = clean_config_text(os.environ.get("MISTRAL_AGENT_ID", ""))
    if env_agent_id:
        return env_agent_id
    cached_version = clean_config_text(read_text(CONFIG_DIR / "mistral_agent_version.txt"))
    if cached_version != AGENT_PROMPT_VERSION:
        return ""
    return clean_config_text(read_text(CONFIG_DIR / "mistral_agent_id.txt"))


def mistral_json_request(api_key, path, payload, timeout=None):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_BASE + path,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        live_log(f"Mistral request started: {path}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            live_log(f"Mistral request completed: {path} HTTP {response.status}")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mistral HTTP {exc.code}: {raw}") from exc


def create_search_agent(api_key, model):
    payload = {
        "model": model,
        "name": "Prairie Semantic Search HTML Operator",
        "description": "Source-first local search operator that returns complete index.html documents.",
        "instructions": OPERATOR_SYSTEM_PROMPT,
        "tools": [{"type": "web_search"}],
        "completion_args": {
            "temperature": 0.15,
            "top_p": 0.9,
            "max_tokens": 12000,
        },
    }
    data = mistral_json_request(api_key, "/v1/agents", payload, timeout=None)
    agent_id = data.get("id") or data.get("agent_id")
    if not agent_id:
        raise RuntimeError("Mistral agent creation response did not include an id.")
    write_text_atomic(CONFIG_DIR / "mistral_agent_id.txt", agent_id + "\n")
    write_text_atomic(CONFIG_DIR / "mistral_agent_version.txt", AGENT_PROMPT_VERSION + "\n")
    live_log("Mistral search agent created and cached.")
    return agent_id


def call_mistral_agent(api_key, agent_id, prompt):
    payload = {
        "agent_id": agent_id,
        "inputs": prompt,
        "stream": False,
    }
    data = mistral_json_request(api_key, "/v1/conversations", payload, timeout=None)
    return extract_best_text(data)


def call_mistral_chat(api_key, model, prompt):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": OPERATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.15,
        "top_p": 0.9,
        "max_tokens": 12000,
        "response_format": {"type": "text"},
    }
    data = mistral_json_request(api_key, "/v1/chat/completions", payload, timeout=None)
    return data["choices"][0]["message"]["content"]


def extract_best_text(data):
    candidates = []

    def walk(value, key=""):
        if isinstance(value, str):
            if key in {"content", "text", "output_text", "answer"} or "<html" in value.lower() or "<!doctype" in value.lower():
                candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, dict):
            for next_key, item in value.items():
                walk(item, next_key)

    walk(data)
    html_candidates = [item for item in candidates if "<html" in item.lower() or "<!doctype" in item.lower()]
    if html_candidates:
        return max(html_candidates, key=len)
    if candidates:
        return max(candidates, key=len)
    return json.dumps(data, indent=2)


def extract_html_document(text):
    text = text.strip()
    fence = re.search(r"```(?:html)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    doctype = re.search(r"<!doctype\s+html.*", text, re.IGNORECASE | re.DOTALL)
    if doctype:
        text = doctype.group(0).strip()
    else:
        html_tag = re.search(r"<html[\s\S]*?</html>", text, re.IGNORECASE)
        if html_tag:
            text = html_tag.group(0).strip()
    if "<html" not in text.lower():
        raise RuntimeError("Mistral did not return a complete HTML document.")
    return text


def html_has_search_contract(document):
    lowered = document.lower()
    has_form = re.search(r"<form[^>]+action=[\"']/search[\"']", document, re.IGNORECASE) is not None
    has_query = re.search(r"<input[^>]+name=[\"']q[\"']", document, re.IGNORECASE) is not None
    has_language = re.search(r"<input[^>]+name=[\"']language[\"']", document, re.IGNORECASE) is not None
    has_button = 'id="language-button"' in lowered or "id='language-button'" in lowered
    has_branding = "powered by mistral" in lowered
    return has_form and has_query and has_language and has_button and has_branding


def insert_before_body_close(document, fragment):
    if re.search(r"</body\s*>", document, re.IGNORECASE):
        return re.sub(r"</body\s*>", lambda _match: fragment + "\n</body>", document, count=1, flags=re.IGNORECASE)
    return document + "\n" + fragment


def insert_after_body_open(document, fragment):
    if re.search(r"<body[^>]*>", document, re.IGNORECASE):
        return re.sub(r"(<body[^>]*>)", lambda match: match.group(1) + "\n" + fragment, document, count=1, flags=re.IGNORECASE)
    return fragment + "\n" + document


def ensure_app_contract(document, query="", language="en"):
    """Keep the model-authored page launchable without interpreting results."""
    if html_has_search_contract(document) and LANGUAGE_SCRIPT_ID in document:
        return document

    query_escaped = html.escape(query, quote=True)
    lang = language if language in {"en", "fr", "zh"} else "en"
    contract_css = """
<style id="semantic-search-contract-style">
  :root { --ss-ink: #202124; --ss-muted: #5f6368; --ss-line: #dadce0; --ss-green: #176b4d; --ss-paper: #fbfcf8; }
  .semantic-search-contract-bar { display: flex; align-items: center; gap: 10px; width: min(720px, calc(100% - 36px)); margin: 18px auto; padding: 8px; border: 1px solid var(--ss-line); border-radius: 999px; background: #fff; box-shadow: 0 2px 12px rgba(23, 107, 77, 0.12); font-family: Arial, Helvetica, sans-serif; }
  .semantic-search-contract-bar input[type="search"] { flex: 1; min-width: 0; min-height: 42px; border: 0; border-radius: 999px; padding: 0 12px; font-size: 16px; color: var(--ss-ink); outline: none; }
  .semantic-search-contract-submit { min-height: 42px; border: 0; border-radius: 999px; padding: 0 18px; background: var(--ss-green); color: #fff; cursor: pointer; white-space: nowrap; }
  .semantic-search-contract-footer { max-width: 860px; margin: 36px auto 20px; padding: 0 18px; color: var(--ss-muted); font: 13px Arial, Helvetica, sans-serif; text-align: center; }
  .semantic-search-language-button { min-height: 42px !important; border: 1px solid var(--ss-line) !important; background: var(--ss-paper) !important; color: var(--ss-ink) !important; border-radius: 999px !important; padding: 0 14px !important; cursor: pointer !important; min-width: 86px !important; }
  @media (max-width: 640px) { .semantic-search-contract-bar { flex-wrap: wrap; border-radius: 22px; } .semantic-search-contract-bar input[type="search"] { flex-basis: 100%; } .semantic-search-contract-submit, .semantic-search-language-button { flex: 1 1 130px; } }
</style>
"""
    contract_form = f"""
<form class="semantic-search-contract-bar" method="post" action="/search" id="search-form">
  <input type="hidden" name="language" id="language-input" value="{html.escape(lang, quote=True)}">
  <input type="search" name="q" id="query-input" value="{query_escaped}" autocomplete="off" aria-label="Search query">
  <button class="semantic-search-contract-submit" id="search-button" type="submit">Prairie Search</button>
  <button class="semantic-search-language-button" id="language-button" type="button">English</button>
</form>
"""
    contract_footer = """
<footer class="semantic-search-contract-footer">Prairie Labs / <strong>Powered by Mistral</strong></footer>
"""
    contract_script = f"""
<script id="{LANGUAGE_SCRIPT_ID}">
  (function () {{
    var labels = [
      {{ code: "en", label: "English", placeholder: "Search the web", button: "Prairie Search" }},
      {{ code: "fr", label: "French", placeholder: "Rechercher sur le web", button: "Recherche Prairie" }},
      {{ code: "zh", label: "Chinese", placeholder: "搜索网页", button: "草原搜索" }}
    ];
    var button = document.getElementById("language-button");
    var input = document.getElementById("language-input");
    var query = document.getElementById("query-input");
    var search = document.getElementById("search-button");
    if (!button || !input) return;
    var current = labels.findIndex(function (item) {{ return item.code === input.value; }});
    if (current < 0) current = 0;
    function applyLanguage() {{
      input.value = labels[current].code;
      button.textContent = labels[current].label;
      if (query) query.placeholder = labels[current].placeholder;
      if (search) search.textContent = labels[current].button;
    }}
    applyLanguage();
    button.addEventListener("click", function () {{
      current = (current + 1) % labels.length;
      applyLanguage();
      try {{ localStorage.setItem("semantic-search-language", labels[current].code); }} catch (err) {{}}
    }});
  }}());
</script>
"""

    if "semantic-search-contract-style" not in document:
        if re.search(r"</head\s*>", document, re.IGNORECASE):
            document = re.sub(r"</head\s*>", lambda _match: contract_css + "\n</head>", document, count=1, flags=re.IGNORECASE)
        else:
            document = contract_css + "\n" + document

    if not re.search(r"<form[^>]+action=[\"']/search[\"']", document, re.IGNORECASE):
        document = insert_after_body_open(document, contract_form)
    elif not re.search(r"<input[^>]+name=[\"']q[\"']", document, re.IGNORECASE):
        document = insert_after_body_open(document, contract_form)

    if not re.search(r"<input[^>]+name=[\"']language[\"']", document, re.IGNORECASE):
        hidden_language = f'<input type="hidden" name="language" id="language-input" value="{html.escape(lang, quote=True)}">'
        form_pattern = r"(<form[^>]+action=[\"']/search[\"'][^>]*>)"
        if re.search(form_pattern, document, re.IGNORECASE):
            document = re.sub(form_pattern, lambda match: match.group(1) + "\n  " + hidden_language, document, count=1, flags=re.IGNORECASE)
        else:
            document = insert_after_body_open(document, hidden_language)

    lowered = document.lower()
    if 'id="language-button"' not in lowered and "id='language-button'" not in lowered:
        document = insert_after_body_open(document, '<button class="semantic-search-language-button" id="language-button" type="button">English</button>')

    if "powered by mistral" not in lowered:
        document = insert_before_body_close(document, contract_footer)

    if LANGUAGE_SCRIPT_ID not in document:
        document = insert_before_body_close(document, contract_script)

    return document


def restore_search_result_contract(document):
    """Recover source-result affordances if the model made them look clickable but not functional."""
    result_pattern = re.compile(
        r"<article\b[^>]*class=[\"'][^\"']*\bresult\b[^\"']*[\"'][^>]*>[\s\S]*?</article>",
        re.IGNORECASE,
    )
    rank = 0

    def score_for_rank(position):
        return max(60, 98 - ((position - 1) * 5))

    def repair_article(match):
        nonlocal rank
        rank += 1
        article = match.group(0)
        article_lower = article.lower()

        if "href=" not in article_lower:
            title_url_pattern = re.compile(
                r"(<h[23][^>]*class=[\"'][^\"']*\bresult-title\b[^\"']*[\"'][^>]*>)([\s\S]*?)(</h[23]>\s*<p[^>]*class=[\"'][^\"']*\bresult-url\b[^\"']*[\"'][^>]*>)(https?://[^<\s]+)(</p>)",
                re.IGNORECASE,
            )

            def title_url_replacement(title_match):
                href = html.unescape(title_match.group(4)).strip()
                href_attr = html.escape(href, quote=True)
                title_html = title_match.group(2).strip()
                url_text = html.escape(href)
                return (
                    f'{title_match.group(1)}<a href="{href_attr}" target="_blank" '
                    f'rel="noopener noreferrer">{title_html}</a>{title_match.group(3)}'
                    f'<a href="{href_attr}" target="_blank" rel="noopener noreferrer">{url_text}</a>'
                    f"{title_match.group(5)}"
                )

            article = title_url_pattern.sub(title_url_replacement, article, count=1)

        has_explicit_rating = (
            re.search(r"class=[\"'][^\"']*\bresult-score\b", article, re.IGNORECASE)
            or re.search(r"\bsemantic\s+rating\s*:", article, re.IGNORECASE)
            or re.search(r"\brelevance\s+score\s*:", article, re.IGNORECASE)
        )
        if not has_explicit_rating:
            score = score_for_rank(rank)
            score_html = f'\n        <p class="result-score">Semantic rating: {score}/100</p>'
            with_score = re.sub(
                r"(<p[^>]*class=[\"'][^\"']*\bresult-url\b[^\"']*[\"'][^>]*>[\s\S]*?</p>)",
                lambda url_match: url_match.group(1) + score_html,
                article,
                count=1,
                flags=re.IGNORECASE,
            )
            if with_score == article:
                with_score = re.sub(
                    r"(</h[23]>)",
                    lambda heading_match: heading_match.group(1) + score_html,
                    article,
                    count=1,
                    flags=re.IGNORECASE,
                )
            article = with_score

        return article

    repaired = result_pattern.sub(repair_article, document)
    if repaired != document and "semantic-result-contract-style" not in repaired:
        result_css = """
<style id="semantic-result-contract-style">
  .result-title a { color: #174ea6; text-decoration: underline; text-underline-offset: 2px; }
  .result-url a { color: #176b4d; text-decoration: none; overflow-wrap: anywhere; }
  .result-score { display: inline-block; margin: 7px 0 4px; padding: 3px 9px; border: 1px solid rgba(23, 107, 77, 0.22); border-radius: 999px; background: rgba(23, 107, 77, 0.08); color: #176b4d; font: 700 12px Arial, Helvetica, sans-serif; }
</style>
"""
        if re.search(r"</head\s*>", repaired, re.IGNORECASE):
            repaired = re.sub(r"</head\s*>", lambda _match: result_css + "\n</head>", repaired, count=1, flags=re.IGNORECASE)
        else:
            repaired = result_css + "\n" + repaired
    return repaired


def operator_prompt(query, language):
    base_html = read_text(BASE_HTML)
    current_html = read_text(INDEX_HTML, base_html)
    timestamp = utc_now().isoformat()
    return f"""USER QUERY:
{query}

DISPLAY LANGUAGE:
{language}

CURRENT UTC TIMESTAMP:
{timestamp}

BASE HTML, NEVER MODIFY THIS FILE ON DISK:
```html
{base_html}
```

CURRENT index.html, MODEL-OWNED WORKING FILE:
```html
{current_html}
```

TASK:
Use your available live web search ability for the query. Then directly author the next complete index.html document.
Every source result must have a blue clickable title rendered as an <a href="https://..."> anchor.
Every visible source URL must also be clickable, or the title and URL must point to the same href.
Every source result must include a visible Semantic rating score like "Semantic rating: 94/100".
Never render blue result titles as plain text. Never render a source URL without a real href nearby.

Return only the complete HTML document. No markdown. No explanation.
"""


OPERATOR_SYSTEM_PROMPT = """You are the Prairie Labs Semantic Search HTML Operator, powered by Mistral Medium 3.5.

You are not writing JSON for another program to parse. You are directly editing index.html.

Core rules:
- Return one complete HTML document only.
- The harness will write your returned document to index.html and refresh the browser.
- base.html is the pristine starting point and must be treated as immutable.
- index.html is your working file. You may rewrite it completely or preserve and alter parts of it.
- Keep a Google-like search-engine feel: centered wordmark, rounded pill search box, compact controls, source links, short snippets, and optional history/status controls.
- Preserve the Prairie Labs theme from base.html: prairie green, sky blue, wheat gold, and clay accents on a calm light page.
- Include visible text: Powered by Mistral.
- Preserve a language button that cycles English -> French -> Chinese.
- Preserve the search form contract: method="post", action="/search", query input name="q", hidden language input name="language".
- Do not show an AI answer block unless the user explicitly asks for an answer.
- Display source-first search results: clickable blue title anchor, clickable direct URL, visible Semantic rating score from 0-100, short source description, and no synthetic summary block above the sources.
- For each result card, use an actual <a href="https://..."> element. Do not use plain blue text or CSS-only fake links.
- Use a clear class name such as result-score for the score so it remains visible and styleable.
- Do not create ads, sponsored placements, affiliate promotion, tracking pixels, cookie banners, or engagement bait.
- If live web search is unavailable, do not invent sources. Return a useful local diagnostic page with the search form intact.
- You may expose local operator buttons by submitting q values: !status, !reset, !shortcut, !open-key, !open-folder, or H.
- Escape user-provided text before embedding it in HTML.
- Use inline CSS and tiny inline JavaScript only; no remote assets.
"""


PRAIRIE_FALLBACK_STYLE = """
:root {
  color-scheme: light;
  --ink: #202124;
  --muted: #5f6368;
  --line: #dadce0;
  --paper: #fbfcf8;
  --white: #ffffff;
  --green: #176b4d;
  --sky: #2f7fbf;
  --wheat: #d99a28;
  --clay: #cf4e2f;
  font-family: Arial, Helvetica, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(180deg, rgba(125, 185, 219, 0.16) 0, rgba(255, 255, 255, 0) 240px),
    linear-gradient(180deg, var(--paper) 0%, #f4f7ee 100%);
  color: var(--ink);
}
.topbar {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 12px;
  padding: 16px 22px;
}
.language-button {
  min-width: 86px;
  min-height: 36px;
  border: 1px solid var(--line);
  background: var(--white);
  color: var(--ink);
  border-radius: 999px;
  padding: 0 14px;
  cursor: pointer;
  font: 13px Arial, Helvetica, sans-serif;
}
main {
  width: min(860px, 100%);
  margin: 0 auto;
  padding: 58px 24px 72px;
}
.wordmark {
  margin: 0 0 16px;
  font-size: clamp(38px, 8vw, 64px);
  line-height: 0.98;
  font-weight: 500;
  letter-spacing: 0;
}
.wordmark .p { color: var(--green); }
.wordmark .r { color: var(--sky); }
.wordmark .a { color: var(--wheat); }
.wordmark .i { color: var(--clay); }
.wordmark .rie { color: var(--green); }
.wordmark .search { color: var(--ink); font-weight: 400; margin-left: 0.12em; }
.wordmark .dot { color: var(--wheat); }
.muted, .hint { color: var(--muted); line-height: 1.55; }
form.search-form, form#search-form {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 26px 0;
}
.search-box {
  flex: 1;
  display: flex;
  align-items: center;
  min-width: 0;
  min-height: 54px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--white);
  box-shadow: 0 2px 12px rgba(23, 107, 77, 0.10);
}
.search-glyph {
  width: 20px;
  height: 20px;
  margin-left: 17px;
  margin-right: 8px;
  border: 2px solid #7a8088;
  border-radius: 50%;
  position: relative;
  flex: 0 0 auto;
}
.search-glyph::after {
  content: "";
  position: absolute;
  width: 8px;
  height: 2px;
  right: -6px;
  bottom: -4px;
  background: #7a8088;
  border-radius: 999px;
  transform: rotate(45deg);
}
input[type=search] {
  flex: 1;
  min-width: 0;
  min-height: 52px;
  border: 0;
  background: transparent;
  padding: 0 16px 0 8px;
  color: var(--ink);
  font-size: 17px;
  outline: none;
}
button {
  min-height: 42px;
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 0 18px;
  background: #f8f9fa;
  color: var(--ink);
  cursor: pointer;
  font: 14px Arial, Helvetica, sans-serif;
}
.search-button, button[type=submit] {
  background: var(--green);
  color: #fff;
}
pre, .panel {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
  box-shadow: 0 1px 4px rgba(32, 33, 36, 0.06);
}
ol, ul { padding-left: 22px; }
li { margin: 10px 0; }
a { color: #174ea6; }
.footer { color: var(--muted); font-size: 13px; margin-top: 28px; }
.footer strong { color: var(--ink); }
@media (max-width: 640px) {
  .topbar { padding: 12px 14px; }
  main { padding: 42px 18px 64px; }
  form.search-form, form#search-form { flex-direction: column; align-items: stretch; }
  .search-box { width: 100%; }
  button { width: 100%; }
}
"""


def build_config_missing_page():
    key_path = CONFIG_DIR / "mistral_api_key.txt"
    escaped_path = html.escape(str(key_path))
    base = read_text(BASE_HTML)
    if "<main>" in base:
        body = f"""
  <main>
    <section class="search-shell" aria-label="Prairie Search setup">
      <h1 class="wordmark" aria-label="Prairie Search">
        <span class="p">P</span><span class="r">r</span><span class="a">a</span><span class="i">i</span><span class="rie">rie</span><span class="search">Search</span><span class="dot">.</span>
      </h1>
      <p class="tagline">Mistral is ready. Add the local API key once, then search from here.</p>
      <div class="panel">
        <p class="hint">Paste your Mistral API key into:</p>
        <p class="hint"><code>{escaped_path}</code></p>
      </div>
      <form method="post" action="/search" id="search-form">
        <input type="hidden" name="language" id="language-input" value="en">
        <div class="search-box">
          <span class="search-glyph" aria-hidden="true"></span>
          <input type="search" name="q" id="query-input" autocomplete="off" placeholder="Search the web" aria-label="Search query">
        </div>
        <div class="button-row">
          <button class="search-button" id="search-button" type="submit">Prairie Search</button>
          <button class="utility-button" type="button" data-command="!open-key">Mistral Key</button>
        </div>
      </form>
      <p class="hint">Powered by Mistral. The local harness is running.</p>
    </section>
  </main>
"""
        return re.sub(r"<main>[\s\S]*?</main>", lambda _match: body, base, count=1)
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Semantic Search setup</title></head>
<body>
  <h1>Semantic Search</h1>
  <p>Paste your Mistral API key into <code>{escaped_path}</code>.</p>
  <p>Powered by Mistral</p>
  <form method="post" action="/search">
    <input type="hidden" name="language" value="en">
    <input type="search" name="q" autofocus>
    <button type="submit">Search</button>
  </form>
</body>
</html>"""


def build_error_page(query, error_text, language="en"):
    query_escaped = html.escape(query)
    error_escaped = html.escape(error_text)
    lang = language if language in {"en", "fr", "zh"} else "en"
    document = f"""<!doctype html>
<html lang="{html.escape(lang, quote=True)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prairie Search</title>
  <style>{PRAIRIE_FALLBACK_STYLE}</style>
</head>
<body>
  <header class="topbar">
    <button class="language-button" id="language-button" type="button">English</button>
  </header>
  <main>
    <h1 class="wordmark" aria-label="Prairie Search">
      <span class="p">P</span><span class="r">r</span><span class="a">a</span><span class="i">i</span><span class="rie">rie</span><span class="search">Search</span><span class="dot">.</span>
    </h1>
    <p class="muted">The local Mistral HTML operator could not complete this query.</p>
    <form class="search-form" method="post" action="/search" id="search-form">
      <input type="hidden" name="language" id="language-input" value="{html.escape(lang, quote=True)}">
      <div class="search-box">
        <span class="search-glyph" aria-hidden="true"></span>
        <input type="search" name="q" id="query-input" value="{query_escaped}" autocomplete="off" autofocus aria-label="Search query">
      </div>
      <button class="search-button" id="search-button" type="submit">Prairie Search</button>
    </form>
    <pre>{error_escaped}</pre>
    <p class="footer">Prairie Labs / <strong>Powered by Mistral</strong></p>
  </main>
</body>
</html>"""
    return ensure_app_contract(document, query, lang)


def write_history(query, language, html_document):
    safe = re.sub(r"[^A-Za-z0-9]+", "-", query).strip("-").lower()[:60] or "query"
    stamp = utc_now().strftime("%Y-%m-%d_%H-%M-%S")
    path = HISTORY_DIR / f"search_{stamp}_{safe}.txt"
    record = [
        "SEARCH HISTORY RECORD",
        "=====================",
        "",
        "TIMESTAMP:",
        f"    {utc_now().isoformat()}",
        "",
        "LANGUAGE:",
        f"    {language}",
        "",
        "QUERY:",
        f"    {query}",
        "",
        "INDEX_HTML:",
        str(INDEX_HTML),
        "",
        "RENDERED_HTML_SNAPSHOT:",
        html_document,
    ]
    write_text_atomic(path, "\n".join(record))


def build_shell_page(title, message, language="en", details=""):
    title_escaped = html.escape(title)
    message_escaped = html.escape(message)
    details_block = f"<pre>{html.escape(details)}</pre>" if details else ""
    lang = language if language in {"en", "fr", "zh"} else "en"
    document = f"""<!doctype html>
<html lang="{html.escape(lang, quote=True)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_escaped}</title>
  <style>{PRAIRIE_FALLBACK_STYLE}</style>
</head>
<body>
  <header class="topbar">
    <button class="language-button" id="language-button" type="button">English</button>
  </header>
  <main>
    <h1 class="wordmark" aria-label="Prairie Search">
      <span class="p">P</span><span class="r">r</span><span class="a">a</span><span class="i">i</span><span class="rie">rie</span><span class="search">Search</span><span class="dot">.</span>
    </h1>
    <h2>{title_escaped}</h2>
    <p class="muted">{message_escaped}</p>
    <form class="search-form" method="post" action="/search" id="search-form">
      <input type="hidden" name="language" id="language-input" value="{html.escape(lang, quote=True)}">
      <div class="search-box">
        <span class="search-glyph" aria-hidden="true"></span>
        <input type="search" name="q" id="query-input" autocomplete="off" autofocus aria-label="Search query">
      </div>
      <button class="search-button" id="search-button" type="submit">Prairie Search</button>
    </form>
    {details_block}
    <p class="footer">Prairie Labs / <strong>Powered by Mistral</strong></p>
  </main>
</body>
</html>"""
    return ensure_app_contract(document, "", lang)


def installed_app_root():
    try:
        return PAYLOAD_DIR.parents[3]
    except IndexError:
        return PAYLOAD_DIR


def create_desktop_shortcut():
    launcher = PAYLOAD_DIR / "Launch-SemanticSearch.ps1"
    ps_payload = str(PAYLOAD_DIR).replace("'", "''")
    ps_launcher = str(launcher).replace("'", "''")
    script = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Semantic Search.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "{ps_launcher}"'
$shortcut.WorkingDirectory = '{ps_payload}'
$shortcut.IconLocation = Join-Path $env:SystemRoot 'System32\\shell32.dll,23'
$shortcut.Description = 'Semantic Search, powered by Mistral'
$shortcut.Save()
Write-Output $shortcutPath
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell shortcut creation failed.")
    return result.stdout.strip()


def open_local_path(path):
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def open_live_log_tail():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LIVE_LOG.exists():
        LIVE_LOG.write_text("", encoding="utf-8")
    live_log("Live log tail requested.")
    if os.name == "nt":
        ps_log = str(LIVE_LOG).replace("'", "''")
        command = (
            "$host.UI.RawUI.WindowTitle = 'Prairie Search Live Logs'; "
            f"Get-Content -LiteralPath '{ps_log}' -Tail 80 -Wait"
        )
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-NoExit",
                "-Command",
                command,
            ],
            cwd=str(PAYLOAD_DIR),
        )
    else:
        subprocess.Popen(["tail", "-f", str(LIVE_LOG)])


def build_status_page(language="en"):
    key_set = bool(get_api_key())
    model = get_model()
    agent_id = clean_config_text(read_text(CONFIG_DIR / "mistral_agent_id.txt"))
    agent_version = clean_config_text(read_text(CONFIG_DIR / "mistral_agent_version.txt"))
    history_count = len(list(HISTORY_DIR.glob("*.txt")))
    log_count = len(list(LOG_DIR.glob("*.txt")))
    details = "\n".join([
        f"Installed app root: {installed_app_root()}",
        f"Payload folder: {PAYLOAD_DIR}",
        f"base.html: {BASE_HTML.exists()}",
        f"index.html: {INDEX_HTML.exists()}",
        f"Mistral key configured: {key_set}",
        f"Mistral model: {model}",
        f"Mistral agent id cached: {bool(agent_id)}",
        f"Mistral agent prompt version: {agent_version or 'not cached'}",
        f"Mistral agent cache current: {agent_version == AGENT_PROMPT_VERSION}",
        f"History records: {history_count}",
        f"Log files: {log_count}",
        "",
        "Local commands:",
        "  !status       show this page",
        "  !reset        reset index.html from base.html",
        "  !shortcut     recreate the desktop shortcut",
        "  !open-key     open the local Mistral key file",
        "  !open-folder  open the payload folder",
        "  !logs         open a live harness log window",
        "  H             show search history",
    ])
    return build_shell_page("Semantic Search Status", "Local harness is running.", language, details)


def handle_local_command(query, language):
    command = query.strip().lower()
    if command in {"!status", ":status"}:
        return build_status_page(language)
    if command in {"!reset", ":reset"}:
        if BASE_HTML.exists():
            return read_text(BASE_HTML)
        return build_shell_page("Semantic Search Reset", "base.html was not found.", language)
    if command in {"!shortcut", ":shortcut"}:
        shortcut = create_desktop_shortcut()
        return build_shell_page("Semantic Search Shortcut", "Desktop shortcut was created or refreshed.", language, shortcut)
    if command in {"!open-key", ":open-key"}:
        key_file = CONFIG_DIR / "mistral_api_key.txt"
        if not key_file.exists():
            write_text_atomic(key_file, "PASTE_MISTRAL_API_KEY_HERE\n")
        open_local_path(key_file)
        return build_shell_page("Semantic Search Key", "Opened the local Mistral API key file.", language, str(key_file))
    if command in {"!open-folder", ":open-folder"}:
        open_local_path(PAYLOAD_DIR)
        return build_shell_page("Semantic Search Folder", "Opened the local payload folder.", language, str(PAYLOAD_DIR))
    if command in {"!logs", ":logs", "!live-log", ":live-log"}:
        open_live_log_tail()
        return build_shell_page("Semantic Search Logs", "Opened the live harness log window.", language, str(LIVE_LOG))
    return None


def run_search(query, language):
    ensure_dirs()
    live_log(f"Search requested: {query!r} language={language!r}")
    if BASE_HTML.exists():
        shutil.copyfile(BASE_HTML, INDEX_HTML)
        live_log("index.html reset from base.html before search.")

    api_key = get_api_key()
    if not api_key:
        live_log("No Mistral API key found; writing setup page.")
        page = build_config_missing_page()
        write_text_atomic(INDEX_HTML, page)
        return

    model = get_model()
    live_log(f"Using Mistral model: {model}")
    prompt = operator_prompt(query, language)
    raw = ""
    agent_id = get_cached_agent_id()

    try:
        if agent_id:
            live_log("Using cached Mistral search agent.")
            raw = call_mistral_agent(api_key, agent_id, prompt)
        else:
            try:
                live_log("No current cached agent; creating Mistral search agent.")
                agent_id = create_search_agent(api_key, model)
                raw = call_mistral_agent(api_key, agent_id, prompt)
            except Exception as agent_error:
                log_error("Agent/web-search path failed; falling back to chat completions.", agent_error)
                live_log("Falling back to chat completions path.")
                raw = call_mistral_chat(api_key, model, prompt)

        live_log("Mistral returned content; extracting complete HTML document.")
        document = restore_search_result_contract(ensure_app_contract(extract_html_document(raw), query, language))
        write_text_atomic(INDEX_HTML, document)
        live_log("index.html written with Mistral-authored document.")
        write_history(query, language, document)
        live_log("Search history snapshot written.")
    except Exception as exc:
        log_error("Search failed.", exc)
        page = build_error_page(query, str(exc), language)
        write_text_atomic(INDEX_HTML, page)
        live_log("Error page written to index.html.")


def render_history_page():
    files = sorted(HISTORY_DIR.glob("*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    rows = []
    for path in files[:50]:
        name = html.escape(path.name)
        rows.append(f"<li><a href=\"/history/{urllib.parse.quote(path.name)}\">{name}</a></li>")
    row_html = "\n".join(rows) or "<li>No history yet.</li>"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prairie Search History</title>
  <style>{PRAIRIE_FALLBACK_STYLE}</style>
</head>
<body>
  <header class="topbar">
    <button class="language-button" id="language-button" type="button">English</button>
  </header>
  <main>
    <h1 class="wordmark" aria-label="Prairie Search">
      <span class="p">P</span><span class="r">r</span><span class="a">a</span><span class="i">i</span><span class="rie">rie</span><span class="search">Search</span><span class="dot">.</span>
    </h1>
    <h2>History</h2>
    <form class="search-form" method="post" action="/search" id="search-form">
      <input type="hidden" name="language" id="language-input" value="en">
      <div class="search-box">
        <span class="search-glyph" aria-hidden="true"></span>
        <input type="search" name="q" id="query-input" autocomplete="off" autofocus placeholder="Search the web">
      </div>
      <button class="search-button" id="search-button" type="submit">Prairie Search</button>
    </form>
    <div class="panel"><ol>{row_html}</ol></div>
    <p class="footer">Prairie Labs / <strong>Powered by Mistral</strong></p>
  </main>
</body>
</html>"""
    write_text_atomic(INDEX_HTML, ensure_app_contract(page, "", "en"))


class SemanticHandler(SimpleHTTPRequestHandler):
    server_version = "SemanticSearchHarness/0.4"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAYLOAD_DIR), **kwargs)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if parsed.path in {"", "/"}:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/index.html")
            self.end_headers()
            return
        if parsed.path.startswith("/history/"):
            name = urllib.parse.unquote(parsed.path.split("/", 2)[2])
            path = HISTORY_DIR / name
            if path.exists() and path.is_file():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(path.read_bytes())
                return
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/search":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        data = urllib.parse.parse_qs(raw)
        query = (data.get("q") or [""])[0].strip()
        language = (data.get("language") or ["en"])[0].strip() or "en"
        live_log(f"POST /search received q={query!r} language={language!r}")

        try:
            local_page = handle_local_command(query, language)
            if local_page:
                write_text_atomic(INDEX_HTML, local_page)
            elif query.upper() == "H" or query.lower() == "history":
                render_history_page()
            elif query:
                run_search(query, language)
            elif BASE_HTML.exists():
                shutil.copyfile(BASE_HTML, INDEX_HTML)
        except Exception as exc:
            log_error("Request failed.", exc)
            write_text_atomic(INDEX_HTML, build_error_page(query, str(exc), language))

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/index.html?v=" + urllib.parse.quote(str(utc_now().timestamp())))
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description="Semantic Search local HTML operator harness")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ensure_dirs()
    live_log(f"Harness starting on http://127.0.0.1:{args.port}/index.html")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), SemanticHandler)
    print(f"{APP_NAME} harness listening on http://127.0.0.1:{args.port}/index.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
