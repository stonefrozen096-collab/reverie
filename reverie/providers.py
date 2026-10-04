"""AI companies that reverie write can talk to: Claude, OpenAI, Gemini and Groq."""
import json
import os
import re
import time
import urllib.error
import urllib.request


class AIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def scrub(text, secret):
    if secret:
        text = text.replace(secret, "[hidden]")
    return re.sub(r"(sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|gsk_[A-Za-z0-9]{16,})", "[hidden]", text)


def explain(error):
    """Plain-language reason for a failed call."""
    status = error.status
    low = error.message.lower()
    if status == 0:
        return error.message + ". Check your internet and try again."
    if status in (401, 403) or "api key not valid" in low or "invalid api key" in low:
        return ("The company did not accept this key. Check that you pasted the whole key, "
                "that it has not been deleted, and that it belongs to this company.")
    if status == 429:
        return ("The company says you are out of free credit or going too fast (429). "
                "Wait a minute and try again. If it keeps happening, check the credit on your account page.")
    if status == 404:
        return "The company could not find that model (404). Pick another model from the list."
    if status == 400:
        return "The company refused the request: " + error.message
    return f"The company said: {error.message} ({status})"


class Provider:
    key = ""
    name = ""
    base = ""
    key_help = ""

    def __init__(self, api_key):
        self.api_key = api_key
        override = os.environ.get("REVERIE_AI_TEST_BASE")
        self.root = (override.rstrip("/") + self.test_prefix) if override else self.base

    test_prefix = ""

    def _call(self, method, url, headers, body=None, retries=3):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(headers)
        headers.setdefault("User-Agent", "reverie")
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                    message = parsed.get("error", parsed)
                    if isinstance(message, dict):
                        message = message.get("message", raw)
                except ValueError:
                    message = raw
                if error.code in (429, 500, 502, 503, 529) and attempt < retries:
                    wait = 5
                    try:
                        wait = min(30, int(float(error.headers.get("Retry-After", "5"))))
                    except (TypeError, ValueError):
                        pass
                    time.sleep(max(1, wait))
                    continue
                raise AIError(error.code, scrub(str(message)[:300], self.api_key))
            except (urllib.error.URLError, OSError) as error:
                if attempt < retries:
                    time.sleep(3)
                    continue
                raise AIError(0, "Could not reach " + self.name + " (" + scrub(str(error), self.api_key) + ")")

    def list_models(self):
        raise NotImplementedError

    def chat(self, model, system, prompt, max_tokens):
        """Returns (text, tokens_in, tokens_out)."""
        raise NotImplementedError


class Claude(Provider):
    key = "claude"
    name = "Claude (Anthropic)"
    base = "https://api.anthropic.com/v1"
    key_help = "Get a key at console.anthropic.com > API keys. Keys start with sk-ant-"
    test_prefix = "/v1"

    def _headers(self):
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    def list_models(self):
        data = self._call("GET", self.root + "/models?limit=100", self._headers())
        return [m["id"] for m in data.get("data", []) if "id" in m]

    def chat(self, model, system, prompt, max_tokens):
        data = self._call("POST", self.root + "/messages", self._headers(), {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": prompt}]})
        text = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        usage = data.get("usage", {})
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


class OpenAICompatible(Provider):
    """OpenAI, and Groq, which uses the same kind of connection."""
    skip_words = ("embedding", "whisper", "tts", "dall-e", "image", "audio", "moderation",
                  "realtime", "transcribe", "guard", "safeguard", "playai", "search", "davinci", "babbage")

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key}

    def list_models(self):
        data = self._call("GET", self.root + "/models", self._headers())
        ids = [m["id"] for m in data.get("data", []) if "id" in m]
        return [i for i in ids if not any(w in i.lower() for w in self.skip_words)]

    def chat(self, model, system, prompt, max_tokens):
        body = {"model": model, "max_completion_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        try:
            data = self._call("POST", self.root + "/chat/completions", self._headers(), body)
        except AIError as error:
            if error.status == 400 and "max_" in error.message.lower():
                body["max_tokens"] = body.pop("max_completion_tokens")
                data = self._call("POST", self.root + "/chat/completions", self._headers(), body)
            else:
                raise
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage", {})
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


class OpenAI(OpenAICompatible):
    key = "openai"
    name = "OpenAI"
    base = "https://api.openai.com/v1"
    key_help = "Get a key at platform.openai.com > API keys. Keys start with sk-"
    test_prefix = "/v1"


class Groq(OpenAICompatible):
    key = "groq"
    name = "Groq"
    base = "https://api.groq.com/openai/v1"
    key_help = "Get a free key at console.groq.com > API Keys. Keys start with gsk_"
    test_prefix = "/openai/v1"


class Gemini(Provider):
    key = "gemini"
    name = "Gemini (Google)"
    base = "https://generativelanguage.googleapis.com/v1beta"
    key_help = "Get a free key at aistudio.google.com > Get API key. Keys start with AIza"
    test_prefix = "/v1beta"

    def _headers(self):
        return {"x-goog-api-key": self.api_key}

    def list_models(self):
        data = self._call("GET", self.root + "/models?pageSize=200", self._headers())
        names = []
        for model in data.get("models", []):
            if "generateContent" in model.get("supportedGenerationMethods", []):
                names.append(model.get("name", "").replace("models/", "", 1))
        return [n for n in names if n and not any(w in n.lower() for w in ("embedding", "aqa", "imagen", "tts", "image"))]

    def chat(self, model, system, prompt, max_tokens):
        data = self._call("POST", self.root + f"/models/{model}:generateContent", self._headers(), {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens}})
        candidates = data.get("candidates") or [{}]
        parts = (candidates[0].get("content") or {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        return text, int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0))


PROVIDERS = [Claude, OpenAI, Gemini, Groq]
