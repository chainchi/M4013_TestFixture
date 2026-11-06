"""Model providers for the agent. The conversation is kept in the Claude
Messages format (content blocks); the OpenAI-compatible provider converts it
on every request, so the user can switch provider mid-conversation."""

import json
import re
import uuid

import aiohttp

# Claude models that take the server-side refusal fallback ("default" mode)
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16000
OPENAI_TIMEOUT_S = 300


class LlmError(Exception):
    pass


class Reply:
    def __init__(self, content, stop, note=None):
        self.content = content         # list of Claude-format block dicts
        self.stop = stop               # "tool_use", "end_turn", "max_tokens", "refusal", ...
        self.note = note               # shown to the user (refusal, truncation)


def tool_id(raw):
    """Tool-use ids must match ^[A-Za-z0-9_-]+$ for Claude."""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", raw or "")
    return s or "call_" + uuid.uuid4().hex[:16]

# ---------- Claude (official SDK) ----------


async def _claude(p, system, tools, history):
    try:
        import anthropic
    except ImportError:
        raise LlmError("the anthropic package is not installed on the Pi (see README, Agent)")
    client = anthropic.AsyncAnthropic(api_key=p["api_key"], base_url=p.get("base_url") or None)
    kw = dict(model=p["model"], max_tokens=MAX_TOKENS, system=system, tools=tools, messages=history,
              cache_control={"type": "ephemeral"})  # system + tools + history prefix is reused every round
    if p.get("effort"):
        kw["output_config"] = {"effort": p["effort"]}
    try:
        async with client:
            if p["model"] in _FALLBACK_MODELS and not p.get("base_url"):
                # a safety decline is retried on a fallback model inside the same call
                r = await client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
            else:
                r = await client.messages.create(**kw)
    except anthropic.AuthenticationError:
        raise LlmError("API key rejected (401)")
    except anthropic.PermissionDeniedError:
        raise LlmError("API key lacks permission for this model (403)")
    except anthropic.NotFoundError:
        raise LlmError(f"model or endpoint not found: {p['model']}")
    except anthropic.RateLimitError:
        raise LlmError("rate limited (429), try again in a minute")
    except anthropic.BadRequestError as e:
        raise LlmError(f"request rejected: {e.message}")
    except anthropic.APIStatusError as e:
        raise LlmError(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise LlmError("cannot reach the API (network)")
    # stored and sent back unchanged (thinking blocks must be replayed as they came)
    content = [b.to_dict(mode="json", exclude_none=True) for b in r.content]
    note = None
    if r.stop_reason == "refusal":
        cat = r.stop_details.category if r.stop_details else None
        note = f"the model declined this request ({cat or 'no category'})"
    elif r.stop_reason == "max_tokens":
        note = "reply cut off at the output limit"
    return Reply(content, r.stop_reason, note)

# ---------- OpenAI-compatible (Chat Completions) ----------


def _oa_messages(system, history):
    out = [{"role": "system", "content": system}]
    for m in history:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        if m["role"] == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user turn: tool results become "tool" messages, images follow as a user message
        parts, images = [], []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                parts.append({"type": "text", "text": b["text"]})
            elif t == "image":
                images.append(b)
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    images += [x for x in c if x.get("type") == "image"]
                    c = "\n".join(x.get("text", "") for x in c if x.get("type") == "text")
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": ("ERROR: " if b.get("is_error") else "") + (c or "")})
        parts += [{"type": "image_url", "image_url": {
            "url": f"data:{i['source']['media_type']};base64,{i['source']['data']}"}} for i in images]
        if parts:
            out.append({"role": "user", "content": parts if images else "\n".join(x["text"] for x in parts)})
    return out


async def _openai(p, system, tools, history):
    url = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    body = {"model": p["model"], "messages": _oa_messages(system, history),
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                for t in tools]}
    if p.get("effort"):
        body["reasoning_effort"] = p["effort"]
    headers = {"Authorization": f"Bearer {p['api_key']}"} if p.get("api_key") else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=OPENAI_TIMEOUT_S)) as s:
            async with s.post(url, json=body, headers=headers) as r:
                text = await r.text()
                if r.status >= 400:
                    raise LlmError(f"HTTP {r.status}: {text[:300]}")
    except aiohttp.ClientError as e:
        raise LlmError(f"cannot reach {url}: {e}")
    except TimeoutError:
        raise LlmError(f"no answer from {url} in {OPENAI_TIMEOUT_S} s")
    try:
        ch = json.loads(text)["choices"][0]
        msg = ch["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise LlmError(f"unexpected reply: {text[:300]}")
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = {"_unparsed": c["function"].get("arguments")}
        content.append({"type": "tool_use", "id": tool_id(c.get("id")), "name": c["function"]["name"],
                        "input": args if isinstance(args, dict) else {}})
    fin = ch.get("finish_reason")
    stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else \
        "max_tokens" if fin == "length" else "end_turn"
    if not content:
        content = [{"type": "text", "text": "(empty reply)"}]
    return Reply(content, stop, "reply cut off at the output limit" if fin == "length" else None)


PROVIDERS = {"anthropic": _claude, "openai": _openai}


async def complete(profile, system, tools, history):
    fn = PROVIDERS.get(profile.get("provider"))
    if not fn:
        raise LlmError(f"unknown provider {profile.get('provider')!r}")
    if not profile.get("model"):
        raise LlmError("no model set in the profile")
    # stored messages carry page-only fields (which model answered); send role + content only
    msgs = [{"role": m["role"], "content": m["content"]} for m in history]
    return await fn(profile, system, tools, msgs)
