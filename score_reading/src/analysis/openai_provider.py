import logging
import os
from typing import Optional

import httpx
import openai

from .llm_provider import LLMProvider
from src.config import load_config

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """
    OpenAI-compatible implementation (OpenAI/Zhipu/proxy Gemini).
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        config = load_config()
        self.config = config
        self.connect_timeout_sec = float(config.get("llm.connect_timeout_sec", 5.0))
        self.read_timeout_sec = float(config.get("llm.read_timeout_sec", 12.0))
        self.max_retry_rounds = int(config.get("llm.max_retries", 3))
        self.max_total_wait_sec = float(config.get("llm.max_total_wait_sec", 20.0))

        raw_key = (
            api_key
            or config.get("llm.api_key")
            or config.get("engines.gemini.api_key")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )

        if not raw_key:
            logger.warning(
                "No API key found for LLM Advisor (checked: llm.api_key, "
                "engines.gemini.api_key, GEMINI_API_KEY, OPENAI_API_KEY)"
            )
        else:
            logger.info(
                "LLM Advisor: Found API key(s) starting with %s",
                str(raw_key)[:8] if isinstance(raw_key, str) else "List",
            )

        if isinstance(raw_key, str):
            self.api_keys = [k.strip() for k in raw_key.split(",") if k.strip()]
        elif isinstance(raw_key, list):
            self.api_keys = [str(k).strip() for k in raw_key if str(k).strip()]
        else:
            self.api_keys = []

        self.current_key_index = 0

        has_gemini_key = any(k.startswith("AIza") for k in self.api_keys)
        gemini_proxy_base = config.get("engines.gemini.api_base") or os.getenv("GEMINI_API_BASE")
        configured_llm_base = config.get("llm.base_url")
        if has_gemini_key and gemini_proxy_base:
            # Prefer Gemini-compatible proxy for AIza keys.
            self.base_url = gemini_proxy_base
        elif has_gemini_key and configured_llm_base and "bigmodel.cn" in configured_llm_base:
            # Avoid sending Gemini keys to default GLM endpoint.
            self.base_url = None
        else:
            self.base_url = configured_llm_base

        self.model = (
            model
            or config.get("llm.model")
            or config.get("engines.gemini.model")
            or "gemini-3-flash-preview"
        )
        self.preferred_gemini_model = (
            config.get("engines.gemini.model")
            or (self.model if "gemini" in self.model.lower() else None)
            or "gemini-3-flash-preview"
        )

        if has_gemini_key and ("gemini" not in self.model.lower()):
            self.model = config.get("engines.gemini.model") or "gemini-3-flash-preview"

        logger.info(
            "LLM Advisor initialized with model=%s, base_url=%s",
            self.model,
            self.base_url or "Default",
        )

        self.client = None
        self.genai_model = None
        self.client_type = "none"

        if not self.api_keys:
            logger.warning("No usable LLM API key found. LLM features will be disabled.")
        else:
            self._init_client()

    def _init_client(self):
        """Initialize provider client for current key."""
        current_key = self.api_keys[self.current_key_index]

        if current_key.startswith("AIza"):
            proxy_base = self.base_url or os.getenv("GEMINI_API_BASE")
            if proxy_base:
                # Proxy mode: many self-hosted Gemini gateways expose OpenAI-compatible API.
                self.client_type = "openai"
                self.client = openai.OpenAI(api_key=current_key, base_url=proxy_base)
                self.genai_model = None
                logger.info("Initialized Gemini-compatible proxy client for Advisor.")
            else:
                self.client_type = "gemini_rest"
                target_model = self.model.replace("models/", "")
                if "gemini" not in target_model.lower():
                    target_model = self.preferred_gemini_model
                self.model = target_model
                self.client = None
                self.genai_model = None
                logger.info("Initialized native Gemini REST client for Advisor (model=%s).", target_model)
        elif "." in current_key and len(current_key) > 20 and not current_key.startswith("sk-"):
            # Zhipu key in id.secret format
            self.client_type = "zhipu"
            zhipu_default_model = self.config.get("llm.zhipu_model") or "glm-4.5-air"
            target_model = self.model or zhipu_default_model
            if (
                "gemini" in target_model.lower()
                or "gpt" in target_model.lower()
                or not target_model.lower().startswith("glm-")
                or target_model.lower() == "glm-4-flash"
            ):
                target_model = zhipu_default_model
            self.client = openai.OpenAI(
                api_key=current_key,
                base_url="https://open.bigmodel.cn/api/paas/v4/",
            )
            self.model = target_model
            self.genai_model = None
            logger.info("Initialized Zhipu client for Advisor (model=%s).", self.model)
        else:
            self.client_type = "openai"
            client_args = {"api_key": current_key}
            if self.base_url:
                client_args["base_url"] = self.base_url
            self.client = openai.OpenAI(**client_args)
            self.genai_model = None
            logger.info("Initialized OpenAI-compatible client for Advisor (model=%s).", self.model)

        masked = f"{current_key[:4]}...{current_key[-4:]}" if len(current_key) > 8 else "***"
        logger.info("LLM client updated to key index %s (%s)", self.current_key_index, masked)

    def _rotate_key(self):
        """Switch to next API key."""
        if len(self.api_keys) <= 1:
            return False
        self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
        self._init_client()
        logger.warning("Rotated to API key #%s", self.current_key_index)
        return True

    def _generate_via_gemini_rest_proxy(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> Optional[str]:
        """Fallback for Gemini-style proxy endpoints that are not OpenAI compatible."""
        if not self.base_url:
            return None

        current_key = self.api_keys[self.current_key_index]
        if not current_key.startswith("AIza"):
            return None

        model = self.model.replace("models/", "").strip()
        if "gemini" not in model.lower():
            model = "gemini-3-flash-preview"

        prompt = f"{system_prompt}\n\n{user_prompt}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        endpoints = [
            f"{self.base_url.rstrip('/')}/v1beta/models/{model}:generateContent",
            f"{self.base_url.rstrip('/')}/v1/models/{model}:generateContent",
        ]

        with httpx.Client(timeout=(max(2.0, self.connect_timeout_sec), max(4.0, self.read_timeout_sec)), follow_redirects=True) as client:
            for endpoint in endpoints:
                try:
                    resp = client.post(endpoint, params={"key": current_key}, json=payload)
                except Exception as exc:
                    logger.warning("Gemini REST proxy request failed (%s): %s", endpoint, exc)
                    continue

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        candidates = data.get("candidates") or []
                        if candidates:
                            content = candidates[0].get("content") or {}
                            parts = content.get("parts") or []
                            texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
                            text = "".join(texts).strip()
                            if text:
                                logger.info("Gemini REST proxy succeeded via %s", endpoint)
                                return text
                    except Exception as exc:
                        logger.warning("Failed to parse Gemini REST response: %s", exc)
                    continue

                if resp.status_code in (404, 405):
                    continue

                if resp.status_code in (401, 403, 429):
                    raise RuntimeError(f"Gemini REST proxy rejected key/status={resp.status_code}")

        return None

    def _generate_via_gemini_google_rest(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        """Direct call to official Gemini REST API with strict timeout."""
        current_key = self.api_keys[self.current_key_index]
        model = self.model.replace("models/", "").strip()
        if "gemini" not in model.lower():
            model = "gemini-3-flash-preview"

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"{system_prompt}\n\n{user_prompt}",
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        with httpx.Client(timeout=(max(2.0, self.connect_timeout_sec), max(4.0, self.read_timeout_sec)), follow_redirects=True) as client:
            resp = client.post(endpoint, params={"key": current_key}, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini REST error status={resp.status_code}")
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise RuntimeError("Gemini REST returned empty candidates")
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            text = "".join([p.get("text", "") for p in parts if isinstance(p, dict)]).strip()
            if not text:
                raise RuntimeError("Gemini REST returned empty text")
            return text

    def generate_response(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
    ) -> str:
        if self.client_type not in ("gemini", "gemini_rest") and not self.client and not self.genai_model:
            raise RuntimeError("LLM client not initialized (missing API key).")

        import random
        import time

        max_retries = int(self.max_retry_rounds)
        if len(self.api_keys) > 1:
            max_retries = max(max_retries, min(3, len(self.api_keys)))
        else:
            max_retries = min(max_retries, 2)
        max_retries = max(1, min(8, max_retries))
        deadline = time.time() + max(8.0, float(self.max_total_wait_sec))
        for attempt in range(max_retries):
            try:
                if self.client_type in ("gemini", "gemini_rest"):
                    return self._generate_via_gemini_google_rest(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                    )

                # OpenAI-compatible branch (OpenAI/Zhipu/Gemini proxy)
                def _chat_create(client_obj, with_json_mode: bool = True):
                    kwargs = {
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": temperature,
                    }
                    if with_json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    return client_obj.chat.completions.create(**kwargs)

                try:
                    response = _chat_create(self.client, with_json_mode=True)
                except Exception as exc:
                    msg = str(exc)
                    if "response_format" in msg:
                        response = _chat_create(self.client, with_json_mode=False)
                    elif (
                        ("404" in msg or "Not Found" in msg)
                        and self.base_url
                        and "/v1" not in self.base_url.rstrip("/")
                    ):
                        # Some proxies expose OpenAI API under /v1.
                        alt_base = self.base_url.rstrip("/") + "/v1"
                        logger.warning("LLM endpoint returned 404, retrying with base_url=%s", alt_base)
                        alt_client = openai.OpenAI(
                            api_key=self.api_keys[self.current_key_index],
                            base_url=alt_base,
                        )
                        try:
                            response = _chat_create(alt_client, with_json_mode=True)
                        except Exception as inner_exc:
                            if "response_format" in str(inner_exc):
                                response = _chat_create(alt_client, with_json_mode=False)
                            elif "404" in str(inner_exc) or "Not Found" in str(inner_exc):
                                rest_text = self._generate_via_gemini_rest_proxy(
                                    system_prompt=system_prompt,
                                    user_prompt=user_prompt,
                                    temperature=temperature,
                                )
                                if rest_text:
                                    return rest_text
                                raise
                            else:
                                raise
                    elif "404" in msg or "Not Found" in msg:
                        # Fallback for Gemini proxy endpoints that only expose Google-style REST API.
                        rest_text = self._generate_via_gemini_rest_proxy(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            temperature=temperature,
                        )
                        if rest_text:
                            return rest_text
                        raise
                    else:
                        raise
                return response.choices[0].message.content or ""
            except Exception as e:
                is_last_attempt = attempt == max_retries - 1
                logger.warning("LLM API error (attempt %s/%s): %s", attempt + 1, max_retries, e)
                if is_last_attempt or time.time() >= deadline:
                    logger.error("LLM retry budget reached. LLM call failed.")
                    raise

                rotated = self._rotate_key()
                if rotated:
                    time.sleep(0.2)
                else:
                    sleep_time = min(1.2, 0.35 * (attempt + 1))
                    logger.info("Waiting %.1fs before retry...", sleep_time)
                    time.sleep(sleep_time)

        raise RuntimeError("LLM generation failed after retries")
