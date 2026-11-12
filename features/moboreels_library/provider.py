"""Signed CDReader requests. Creation is deliberately never retried here."""
import hashlib
import threading
import time
import uuid
import requests


class ProviderError(RuntimeError):
    pass


class ProviderClient:
    def __init__(self, settings, http=None):
        self.settings = settings
        self.http = http or requests
        self._rate_lock = threading.Lock()
        self._next_request = 0.0

    def _post(self, path, payload, request_id=None):
        if not self.settings.institution_user_id or not self.settings.token:
            raise ProviderError("供应方接口凭证未配置")
        with self._rate_lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + self.settings.api_interval
        request_id = request_id or uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))
        source = "{}_{}_{}_{}".format(request_id, timestamp, self.settings.institution_user_id, self.settings.token).lower()
        headers = {
            "reqid": request_id, "timestamp": timestamp,
            "instuserid": self.settings.institution_user_id,
            "sign": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
        return self.http.post(self.settings.api_base + path, headers=headers, json=payload,
                              timeout=(10, 60), allow_redirects=False)

    def get_page(self, page_index, page_size=100):
        for attempt in range(3):
            try:
                response = self._post("/api/story/getlist", {
                    "project_type": 2, "status": 0, "page_index": page_index, "page_size": page_size,
                })
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.RequestException("temporary upstream response")
                payload = response.json()
                if response.status_code != 200 or payload.get("code") != 200:
                    raise ProviderError("剧目目录接口拒绝请求（HTTP {}，业务码 {}）".format(
                        response.status_code, str(payload.get("code"))[:32]))
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                    raise ProviderError("剧目目录接口返回格式异常")
                return data
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise ProviderError("剧目目录接口暂时不可用，请稍后重试") from None
                time.sleep(2 ** (attempt + 1))

    def create_promotion(self, payload, request_id):
        # The caller durably saves intent before this one network request.
        response = self._post("/api/story/getcodeinfo", payload, request_id)
        try:
            body = response.json()
        except ValueError:
            body = None
        return {"http_status": response.status_code, "response": body}
