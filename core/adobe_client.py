import json
import logging
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from core.config_mgr import config_manager
from core.models import build_image_payload_candidates

try:
    from curl_cffi.requests import Session as CurlSession
except Exception:
    CurlSession = None


logger = logging.getLogger("adobe2api")


class AdobeRequestError(Exception):
    pass


class QuotaExhaustedError(AdobeRequestError):
    pass


class AuthError(AdobeRequestError):
    pass


class UpstreamTemporaryError(AdobeRequestError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_type: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = str(error_type or "").strip().lower()


class AdobeClient:
    submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
    video_submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async"
    upload_url = "https://firefly-3p.ff.adobe.io/v2/storage/image"

    def __init__(self) -> None:
        self.api_key = "clio-playground-web"
        self.impersonate = "chrome124"
        self.proxy = ""
        self.generate_timeout = 300
        self.retry_enabled = True
        self.retry_max_attempts = 3
        self.retry_backoff_seconds = 1.0
        self.retry_on_status_codes = [429, 451, 500, 502, 503, 504]
        self.retry_on_error_types = {"timeout", "connection", "proxy"}
        self.token_rotation_strategy = "round_robin"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        self.sec_ch_ua = (
            '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
        )

        self.apply_config(config_manager.get_all())

        env_api_key = os.getenv("ADOBE_API_KEY")
        env_impersonate = os.getenv("ADOBE_IMPERSONATE")
        env_proxy = os.getenv("ADOBE_PROXY")
        env_user_agent = os.getenv("ADOBE_USER_AGENT")
        env_sec_ch_ua = os.getenv("ADOBE_SEC_CH_UA")
        env_generate_timeout = os.getenv("ADOBE_GENERATE_TIMEOUT")

        if env_api_key:
            self.api_key = env_api_key.strip() or self.api_key
        if env_impersonate:
            self.impersonate = env_impersonate.strip() or self.impersonate
        if env_proxy is not None:
            self.proxy = env_proxy.strip()
        if env_user_agent:
            self.user_agent = env_user_agent.strip() or self.user_agent
        if env_sec_ch_ua:
            self.sec_ch_ua = env_sec_ch_ua.strip() or self.sec_ch_ua
        if env_generate_timeout:
            try:
                self.generate_timeout = int(env_generate_timeout)
                if self.generate_timeout <= 0:
                    self.generate_timeout = 300
            except Exception:
                pass

    def apply_config(self, cfg: dict) -> None:
        proxy = str(cfg.get("proxy", "")).strip()
        use_proxy = bool(cfg.get("use_proxy", False))
        timeout_val = cfg.get("generate_timeout", 300)
        try:
            timeout_val = int(timeout_val)
        except Exception:
            timeout_val = 300
        self.generate_timeout = timeout_val if timeout_val > 0 else 300
        self.proxy = proxy if use_proxy and proxy else ""
        self.retry_enabled = bool(cfg.get("retry_enabled", True))
        try:
            attempts = int(cfg.get("retry_max_attempts", 3))
        except Exception:
            attempts = 3
        self.retry_max_attempts = max(1, min(attempts, 10))

        try:
            backoff = float(cfg.get("retry_backoff_seconds", 1.0))
        except Exception:
            backoff = 1.0
        self.retry_backoff_seconds = max(0.0, min(backoff, 30.0))

        status_codes_raw = cfg.get(
            "retry_on_status_codes", [429, 451, 500, 502, 503, 504]
        )
        parsed_status_codes: list[int] = []
        if isinstance(status_codes_raw, list):
            for item in status_codes_raw:
                try:
                    val = int(item)
                except Exception:
                    continue
                if 100 <= val <= 599:
                    parsed_status_codes.append(val)
        self.retry_on_status_codes = sorted(set(parsed_status_codes)) or [
            429,
            451,
            500,
            502,
            503,
            504,
        ]

        error_types_raw = cfg.get(
            "retry_on_error_types", ["timeout", "connection", "proxy"]
        )
        parsed_error_types: set[str] = set()
        if isinstance(error_types_raw, list):
            for item in error_types_raw:
                txt = str(item or "").strip().lower()
                if txt:
                    parsed_error_types.add(txt)
        self.retry_on_error_types = parsed_error_types or {
            "timeout",
            "connection",
            "proxy",
        }

        strategy = (
            str(cfg.get("token_rotation_strategy", "round_robin") or "round_robin")
            .strip()
            .lower()
        )
        if strategy not in {"round_robin", "random"}:
            strategy = "round_robin"
        self.token_rotation_strategy = strategy
        if self.proxy:
            logger.warning("proxy enabled for upstream requests: %s", self.proxy)
        else:
            logger.warning("proxy disabled for upstream requests")

    def _retry_delay_for_attempt(self, attempt: int) -> float:
        base = float(self.retry_backoff_seconds or 0.0)
        if base <= 0:
            return 0.0
        safe_attempt = max(1, int(attempt))
        return min(30.0, base * (2 ** (safe_attempt - 1)))

    def should_retry_temporary_error(self, exc: UpstreamTemporaryError) -> bool:
        if not self.retry_enabled:
            return False
        if isinstance(exc, UpstreamTemporaryError):
            if exc.status_code is not None:
                try:
                    return int(exc.status_code) in set(self.retry_on_status_codes)
                except Exception:
                    return False
            if exc.error_type:
                return exc.error_type in set(self.retry_on_error_types)
        return False

    @staticmethod
    def _classify_network_error_type(exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        if "timed out" in text or "timeout" in text:
            return "timeout"
        if "proxy" in text:
            return "proxy"
        if (
            "connection" in text
            or "dns" in text
            or "resolve" in text
            or "refused" in text
            or "reset" in text
            or "unreachable" in text
        ):
            return "connection"
        return "network"

    def _requests_proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _session(self):
        if CurlSession is None:
            return None
        kwargs = {"impersonate": self.impersonate, "timeout": 60}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return CurlSession(**kwargs)

    def _browser_headers(self) -> dict:
        return {
            "user-agent": self.user_agent,
            "origin": "https://firefly.adobe.com",
            "referer": "https://firefly.adobe.com/",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def _submit_headers(self, token: str) -> dict:
        headers = self._browser_headers()
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "x-api-key": self.api_key,
                "content-type": "application/json",
                "accept": "*/*",
            }
        )
        return headers

    def _submit_headers_minimal(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "*/*",
        }

    def _poll_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "accept": "*/*",
            "referer": "https://firefly.adobe.com/",
            "origin": "https://firefly.adobe.com",
            "user-agent": self.user_agent,
        }

    def _post_json(self, url: str, headers: dict, payload: dict):
        session = self._session()
        if session is None:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.post(url, headers=headers, json=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        if resp.status_code == 451:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", status_code=451, error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", status_code=451, error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}",
                    status_code=451,
                    error_type="connection",
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}",
                    status_code=451,
                    error_type="network",
                )
        return resp

    def _post_bytes(self, url: str, headers: dict, payload: bytes):
        session = self._session()
        if session is None:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.post(url, headers=headers, data=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _get(self, url: str, headers: dict, timeout: int = 60):
        session = self._session()
        if session is None:
            try:
                return requests.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.get(url, headers=headers)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def upload_image(
        self, token: str, image_bytes: bytes, mime_type: str = "image/jpeg"
    ) -> str:
        if not image_bytes:
            raise AdobeRequestError("image is empty")

        headers = {
            "authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": mime_type,
            "accept": "application/json",
        }
        resp = self._post_bytes(self.upload_url, headers=headers, payload=image_bytes)

        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code != 200:
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"upload image failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"upload image failed: {resp.status_code} {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception:
            raise AdobeRequestError("upload image failed: invalid response")

        image_id = (((data.get("images") or [{}])[0]) or {}).get("id")
        if not image_id:
            raise AdobeRequestError("upload image succeeded but no image id returned")
        return str(image_id)

    def _build_payload_candidates(
        self,
        prompt: str,
        aspect_ratio: str,
        output_resolution: str,
        upstream_model_id: str,
        upstream_model_version: str,
        source_image_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        return build_image_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            source_image_ids=source_image_ids,
        )

    @staticmethod
    def _video_size(aspect_ratio: str, resolution: str = "720p") -> dict:
        res = str(resolution or "720p").lower()
        if res == "1080p":
            if aspect_ratio == "16:9":
                return {"width": 1920, "height": 1080}
            return {"width": 1080, "height": 1920}
        if aspect_ratio == "16:9":
            return {"width": 1280, "height": 720}
        return {"width": 720, "height": 1280}

    @staticmethod
    def _coerce_progress_percent(value: Any) -> Optional[float]:
        if value is None:
            return None

        val: Optional[float] = None
        if isinstance(value, (int, float)):
            val = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("%"):
                text = text[:-1].strip()
            try:
                val = float(text)
            except Exception:
                return None
        elif isinstance(value, dict):
            for key in (
                "progress",
                "percentage",
                "percent",
                "task_progress",
                "taskProgress",
                "value",
            ):
                nested = AdobeClient._coerce_progress_percent(value.get(key))
                if nested is not None:
                    return nested
            return None
        else:
            return None

        if val <= 1.0:
            val = val * 100.0
        if val < 0:
            return 0.0
        if val > 100:
            return 100.0
        return val

    @staticmethod
    def _is_in_progress_status(status_val: str) -> bool:
        return str(status_val or "").upper() in {
            "IN_PROGRESS",
            "RUNNING",
            "PROCESSING",
            "PENDING",
            "QUEUED",
            "STARTED",
        }

    def _extract_progress_percent(self, latest: dict, poll_resp) -> Optional[float]:
        if not isinstance(latest, dict):
            latest = {}

        task_obj = latest.get("task") if isinstance(latest.get("task"), dict) else {}
        result_obj = (
            latest.get("result") if isinstance(latest.get("result"), dict) else {}
        )
        meta_obj = latest.get("meta") if isinstance(latest.get("meta"), dict) else {}
        metadata_obj = (
            latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
        )

        candidates: list[Any] = [
            latest.get("progress"),
            latest.get("percentage"),
            latest.get("percent"),
            latest.get("task_progress"),
            latest.get("taskProgress"),
            task_obj.get("progress"),
            task_obj.get("percentage"),
            result_obj.get("progress"),
            result_obj.get("percentage"),
            meta_obj.get("progress"),
            metadata_obj.get("progress"),
            poll_resp.headers.get("x-task-progress"),
            poll_resp.headers.get("x-progress"),
            poll_resp.headers.get("progress"),
        ]

        for raw in candidates:
            parsed = self._coerce_progress_percent(raw)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _normalize_video_poll_url(raw_url: str) -> str:
        if not raw_url:
            return raw_url
        try:
            parsed = urlparse(raw_url)
            host = parsed.netloc
            path_parts = [p for p in parsed.path.split("/") if p]
            if not host or not path_parts:
                return raw_url
            if not host.startswith("firefly-epo"):
                return raw_url
            job_id = path_parts[-1]
            if not job_id:
                return raw_url
            return f"https://bks-epo8522.adobe.io/v2/jobs/result/{job_id}?host={host}/"
        except Exception:
            return raw_url

    @staticmethod
    def _extract_job_id(raw_url: str) -> str:
        try:
            parsed = urlparse(str(raw_url or ""))
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts:
                return path_parts[-1]
        except Exception:
            pass
        return ""

    @staticmethod
    def _build_video_prompt_json(
        prompt: str, duration: int, negative_prompt: str = ""
    ) -> str:
        payload = {
            "id": 1,
            "duration_sec": int(duration),
            "prompt_text": prompt,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        return json.dumps(payload, ensure_ascii=False)

    def _build_video_payload(
        self,
        video_conf: dict,
        prompt: str,
        aspect_ratio: str,
        duration: int,
        source_image_ids: Optional[list[str]] = None,
        negative_prompt: str = "",
        generate_audio: bool = True,
        reference_mode: str = "frame",
    ) -> dict:
        seed_val = int(time.time()) % 999999
        engine = str(video_conf.get("engine") or "sora2")
        upstream_model = str(
            video_conf.get("upstream_model") or "openai:firefly:colligo:sora2"
        )
        resolution = str(video_conf.get("resolution") or "720p")
        if engine in {"veo31-fast", "veo31-standard"}:
            model_version = (
                "3.1-fast-generate" if engine == "veo31-fast" else "3.1-generate"
            )
            payload = {
                "n": 1,
                "seeds": [seed_val],
                "modelId": "veo",
                "modelVersion": model_version,
                "output": {"storeInputs": True},
                "prompt": prompt,
                "size": self._video_size(aspect_ratio, resolution),
                "generateAudio": bool(generate_audio),
                "referenceBlobs": [],
                "generationMetadata": {"module": "text2video"},
                "modelSpecificPayload": {
                    "parameters": {
                        "durationSeconds": int(duration),
                        "aspectRatio": aspect_ratio,
                        "addWaterMark": False,
                    }
                },
            }
            if source_image_ids:
                if engine == "veo31-standard" and str(reference_mode) == "image":
                    for image_id in source_image_ids[:3]:
                        payload["referenceBlobs"].append(
                            {
                                "id": str(image_id),
                                "usage": "asset",
                            }
                        )
                else:
                    for idx, image_id in enumerate(source_image_ids[:2], start=1):
                        payload["referenceBlobs"].append(
                            {
                                "id": str(image_id),
                                "usage": "general",
                                "promptReference": idx,
                            }
                        )
            return payload

        payload = {
            "n": 1,
            "seeds": [seed_val],
            "modelId": "sora",
            "modelVersion": "sora-2",
            "size": self._video_size(aspect_ratio, resolution),
            "duration": int(duration),
            "fps": 24,
            "prompt": self._build_video_prompt_json(
                prompt=prompt, duration=duration, negative_prompt=negative_prompt
            ),
            "generationMetadata": {"module": "text2video"},
            "model": upstream_model,
            "generateAudio": bool(generate_audio),
            "generateLoop": False,
            "transparentBackground": False,
            "seed": str(seed_val),
            "locale": "en-US",
            "camera": {
                "angle": "none",
                "shotSize": "none",
                "motion": None,
                "promptStyle": None,
            },
            "negativePrompt": negative_prompt or "",
            "jobMode": "standard",
            "debugGenerationEndpoint": "",
            "referenceBlobs": [],
            "referenceFrames": [],
            "referenceImages": [],
            "referenceVideo": None,
            "cameraMotionReferenceVideo": None,
            "characterReference": None,
            "editReferenceVideo": None,
            "output": {"storeInputs": True},
        }
        if source_image_ids:
            first_id = str(source_image_ids[0])
            payload["referenceBlobs"] = [
                {"id": first_id, "usage": "general", "promptReference": 1}
            ]
            reference_frames = [{"localBlobRef": first_id}, None]
            if engine == "veo31-fast" and len(source_image_ids) > 1:
                last_id = str(source_image_ids[1])
                payload["referenceBlobs"].append(
                    {"id": last_id, "usage": "general", "promptReference": 2}
                )
                reference_frames[1] = {"localBlobRef": last_id}
            payload["referenceFrames"] = reference_frames
        return payload

    def generate_video(
        self,
        token: str,
        video_conf: dict,
        prompt: str,
        aspect_ratio: str = "9:16",
        duration: int = 12,
        source_image_ids: Optional[list[str]] = None,
        timeout: int = 600,
        negative_prompt: str = "",
        generate_audio: bool = True,
        reference_mode: str = "frame",
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[bytes, dict]:
        payload = self._build_video_payload(
            video_conf=video_conf,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=duration,
            source_image_ids=source_image_ids,
            negative_prompt=negative_prompt,
            generate_audio=generate_audio,
            reference_mode=reference_mode,
        )
        submit_resp = self._post_json(
            self.video_submit_url, headers=self._submit_headers(token), payload=payload
        )

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or (
            (submit_data.get("links") or {}).get("result") or {}
        ).get("href")
        if not poll_url:
            raise AdobeRequestError("video submit succeeded but no poll url returned")
        poll_url = self._normalize_video_poll_url(str(poll_url))
        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        while True:
            poll_resp = self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
            )
            if poll_resp.status_code in (401, 403):
                raise AuthError("Token invalid or expired")
            if poll_resp.status_code != 200:
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(
                        f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                video_url = ((outputs[0] or {}).get("video") or {}).get("presignedUrl")
                if not video_url:
                    raise AdobeRequestError("video job finished without video url")
                video_resp = self._get(video_url, headers={"accept": "*/*"}, timeout=60)
                video_resp.raise_for_status()
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                return video_resp.content, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"video job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"video job failed: {latest}")

            if time.time() - start > timeout:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if "progress_val" in locals()
                                and progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "video generation timed out",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError("video generation timed out")
            time.sleep(3.0)

    def generate(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_resolution: str = "2K",
        upstream_model_id: str = "gemini-flash",
        upstream_model_version: str = "nano-banana-2",
        source_image_ids: Optional[list[str]] = None,
        timeout: int = 180,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[bytes, dict]:
        submit_resp = None
        last_error = ""
        for payload in self._build_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            source_image_ids=source_image_ids,
        ):
            submit_resp = self._post_json(
                self.submit_url, headers=self._submit_headers(token), payload=payload
            )
            if submit_resp.status_code == 200:
                break

            if submit_resp.status_code in (401, 403):
                break

            last_error = submit_resp.text[:300]

        if submit_resp is None:
            raise AdobeRequestError("submit failed: no response")

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            logger.warning(
                "submit auth failed status=%s access_error=%s body=%s",
                submit_resp.status_code,
                access_error,
                submit_resp.text[:300],
            )
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            logger.error(
                "submit failed status=%s body=%s",
                submit_resp.status_code,
                submit_resp.text[:500],
            )
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            if last_error:
                raise AdobeRequestError(
                    f"submit failed: {submit_resp.status_code} {last_error}"
                )
            raise AdobeRequestError(
                f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = submit_resp.headers.get("x-override-status-link") or (
            (submit_data.get("links") or {}).get("result") or {}
        ).get("href")
        if not poll_url:
            raise AdobeRequestError("submit succeeded but no poll url returned")

        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        latest = {}
        sleep_time = 3.0
        while True:
            poll_resp = self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
            )
            if poll_resp.status_code != 200:
                logger.error(
                    "poll failed status=%s body=%s",
                    poll_resp.status_code,
                    poll_resp.text[:500],
                )
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(
                        f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl")
                if not image_url:
                    raise AdobeRequestError("job finished without image url")
                img_resp = self._get(image_url, headers={"accept": "*/*"}, timeout=30)
                img_resp.raise_for_status()
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                return img_resp.content, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"image job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"image job failed: {latest}")

            if time.time() - start > timeout:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "image generation timed out",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError("generation timed out")
            time.sleep(sleep_time)
