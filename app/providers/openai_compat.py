"""OpenAI 兼容协议的词典 Provider。

使用裸 httpx 而非官方 SDK：自建网关对 SDK 的版本与参数支持良莠不齐，
直接构造 HTTP 请求的兼容面最大。
"""

from __future__ import annotations

import json
import re
import time

import httpx

from ..core.models import Entry, Phrase
from .base import NonRetryableError, ProviderError
from .prompts import (
    PHRASE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_phrase_prompt,
    build_user_prompt,
)

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def extract_json(text: str) -> dict:
    """从模型输出中稳健地取出 JSON 对象。

    依次尝试：直接解析 → 剥离 Markdown 围栏 → 截取首尾大括号之间的内容。
    """
    text = (text or "").strip()
    if not text:
        raise ProviderError("模型返回内容为空")

    candidates = [text]
    stripped = _FENCE_RE.sub("", text).strip()
    if stripped != text:
        candidates.append(stripped)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except ValueError:
            pass

    raise ProviderError("模型返回内容无法解析为 JSON")


def build_endpoint(base_url: str) -> str:
    """把用户填写的 base_url 归一化为 chat/completions 端点。

    这里多挡两道，目的都是把「配置写错」变成一句人话，而不是等到 httpx 抛出
    UnsupportedProtocol 那种看不出所以然的异常（甚至更早就 AttributeError）：
    - 值不是字符串：配置文件被手改坏时会出现
    - 漏写协议：用户填成 `api.deepseek.com/v1`
    """
    if base_url is not None and not isinstance(base_url, str):
        raise ProviderError(f"API 地址无效（应为文本）：{base_url!r}")
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ProviderError("未配置 API 地址，请在设置中填写")
    if not url.lower().startswith(("http://", "https://")):
        raise ProviderError(f"API 地址需要以 http:// 或 https:// 开头：{url}")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


# 确定性失败：把状态码翻译成用户能据以动手的提示。
# 这些错误重试多少次结果都一样，必须走 NonRetryableError 而不是 ProviderError。
DETERMINISTIC_STATUS_HINTS = {
    400: "请求被拒绝，通常是模型名称不被支持，或请求参数有误",
    402: "账户额度不足或计费异常，请检查服务商的账户状态",
    403: "访问被拒绝，请确认密钥权限，或所在地区是否受限",
    422: "请求参数不被接受，请检查模型名称与接口地址是否匹配",
}


def describe_network_error(exc: Exception, timeout: float) -> str:
    """把 httpx 的网络异常翻译成用户能看懂的话。

    未翻译时用户看到的是原始异常串（超时会被显示成 "timed out"，
    连接失败是英文的底层错误），既不知道发生了什么，也不知道该改什么。
    """
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"请求超时（{timeout:g} 秒）。可能是网络不稳定，或接口响应过慢；"
            "若一直如此，请确认接口地址是否可达。"
        )
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到接口地址。请检查设置中的接口地址是否正确、网络是否可用。"
    return "网络请求失败，请检查网络连接后重试。"


def extract_error_message(text: str, limit: int = 160) -> str:
    """从错误响应体里取出人类可读的说明。

    直接把整个响应体塞给用户会得到一长串 JSON（还常被截断在句子中间）。
    标准错误信封是 {"error": {"message": ...}}，把 message 取出来即可；
    非 JSON（网关返回的 HTML 错误页等）则剥掉标签后退回纯文本摘要。
    """
    raw = (text or "").strip()
    if not raw:
        return ""

    def clip(value: str) -> str:
        flat = " ".join(str(value).split())
        return flat[:limit] + ("…" if len(flat) > limit else "")

    try:
        data = json.loads(raw)
    except ValueError:
        return clip(re.sub(r"<[^>]+>", " ", raw))

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return clip(err["message"])
        if isinstance(err, str) and err.strip():
            return clip(err)
        if data.get("message"):
            return clip(data["message"])
    return ""


class OpenAICompatProvider:
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))

    def _request(self, word: str) -> str:
        return self._chat(SYSTEM_PROMPT, build_user_prompt(word))

    def _request_phrase(self, phrase: str) -> str:
        return self._chat(PHRASE_SYSTEM_PROMPT, build_phrase_prompt(phrase))

    def _chat(self, system: str, user: str) -> str:
        """发一次对话请求并取回正文。

        词条与词组**共用这一处**：请求体、鉴权头、超时与网络异常翻译只有一份
        定义。各写一份的后果不是代码重复，而是两份会慢慢分叉——某天只给其中
        一份加了超时或改了错误文案，另一条路径就悄悄用着旧行为。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = client.post(
                    build_endpoint(self.base_url), json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                # 网络层异常在此统一翻译，避免原始英文异常直接抛到界面上
                raise ProviderError(describe_network_error(exc, self.timeout)) from exc

        return self._read_content(resp)

    def _read_content(self, resp: httpx.Response, *, require_choices: bool = True) -> str:
        """校验状态码并取出 choices[0].message.content。

        重试与否由异常类型决定：NonRetryableError 表示确定性失败，
        ProviderError 表示可重试。除 401 / 404 外，400 / 402 / 403 / 422
        同样是确定性失败——模型名写错这种问题再试一百次也一样失败。

        `require_choices=False` 给「测试连接」用（见 `probe`）：那一步要回答的
        只是「这个地址与密钥能不能通」，**不该因为响应里没有 `choices` 就说
        连不上**——有些网关在探测类请求上会回一个别的结构，而地址明明是通的。
        状态码与错误文案的翻译两个分支共用，只有「要不要解析成功响应」这一处
        分岔。
        """
        status = resp.status_code
        if status == 401:
            raise NonRetryableError("API 鉴权失败（401），请检查密钥是否正确、是否已过期。")
        if status == 404:
            raise NonRetryableError(
                f"接口地址不存在（404）：{build_endpoint(self.base_url)}"
            )

        detail = extract_error_message(resp.text)
        suffix = f"　服务端说明：{detail}" if detail else ""

        if status in DETERMINISTIC_STATUS_HINTS:
            raise NonRetryableError(f"{DETERMINISTIC_STATUS_HINTS[status]}（{status}）。{suffix}")
        if status == 429:
            # 限流是暂时性的，重试有意义
            raise ProviderError(f"请求过于频繁（429），请稍后再试。{suffix}")
        if status >= 500:
            raise ProviderError(f"服务端异常（{status}），已重试仍未成功。{suffix}")
        if status >= 400:
            raise NonRetryableError(f"请求失败（{status}）。{suffix}")

        if not require_choices:
            return ""

        try:
            body = resp.json()
            return body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"响应结构非预期：{exc}") from exc

    def probe(self) -> str:
        """发一个最小请求，确认「地址 + 密钥 + 模型名」这一组配置真的能用。

        与 `lookup()` 的区别是**它不查词**：不带词典提示词、不解析 JSON、
        `max_tokens` 只要 1。它要回答的唯一问题是「这个配置通不通」，而用一次
        真查词去回答，既慢又会把「模型没按格式回 JSON」这类与配置无关的问题
        混进结论里——那种情况下用户会去改地址，而问题其实在别处。

        **刻意不带 `temperature`**：推理型模型（deepseek-reasoner / R1 系列）
        不接受这个参数，带上会直接 400，而那并不是用户配置错了。查词路径必须
        带它（要稳定的输出），所以这里的 payload 与 `_request` 不共用。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            try:
                resp = client.post(
                    build_endpoint(self.base_url), json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                raise ProviderError(describe_network_error(exc, self.timeout)) from exc

        self._read_content(resp, require_choices=False)
        try:
            body = resp.json()
        except ValueError:
            return f"接口有响应，但返回的不是 JSON（模型名 {self.model}）"
        if not isinstance(body, dict) or "choices" not in body:
            return f"接口有响应，但结构不像 OpenAI 兼容（响应里没有 choices）"
        return f"连接正常 · {self.model}"

    def lookup(self, word: str) -> Entry:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                content = self._request(word)
                entry = Entry.from_payload(extract_json(content))
                if not entry.lemma:
                    entry.lemma = word
                if not entry.definition_en and not entry.definition_zh:
                    raise ProviderError("模型未返回任何释义")
                return entry
            except NonRetryableError:
                # 鉴权失败、地址错误等，重试不会改变结果
                raise
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(0.6 * (attempt + 1))
        raise ProviderError(str(last_error) if last_error else "查词失败")

    def lookup_phrase(self, phrase: str) -> Phrase:
        """按词组查一次。

        重试策略与 `lookup` 完全一致（同一套 `max_retries`、同样的
        NonRetryableError 直通），差别只在提示词与解析。**空壳拒收也在这里**：
        没有中文简释的返回等于请求失败，写进词组收藏只会多一条只有标题的空卡。
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                content = self._request_phrase(phrase)
                result = Phrase.from_payload(extract_json(content), phrase)
                if not result.gloss_zh:
                    raise ProviderError("模型没有返回词组的释义")
                return result
            except NonRetryableError:
                raise
            except (httpx.HTTPError, ProviderError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(0.6 * (attempt + 1))
        raise ProviderError(str(last_error) if last_error else "词组查询失败")


def provider_status(config: dict, api_key: str) -> str:
    """描述当前接入状态，供界面常驻展示。

    这里必须能区分「主动选择离线」「选了接口但没填密钥」与「已接入接口」。
    中间那种是最容易让人误解的状态：界面照常出结果，但内容其实不是接口给的，
    用户会以为接口已经通了。把状态摆在界面上，比等用户来问「为什么查不到」要好。
    """
    mode = config.get("provider")
    if mode == "offline":
        from .offline import offline_dict_status, resolve_offline_dict

        return offline_dict_status(resolve_offline_dict())
    if mode == "mock":
        return "离线示例模式（仅演示词条）"
    if not api_key:
        # 选了接口却没填密钥：与其退回只有 3 个词的示例，不如用真词典兜住。
        from .offline import offline_dict_status, resolve_offline_dict

        offline = resolve_offline_dict()
        if offline is not None:
            return f"{offline_dict_status(offline)} · 未填写 API 密钥"
        return "离线示例模式 · 未填写 API 密钥"
    model = (config.get("model") or "").strip()
    base = f"已接入接口 · {model}" if model else "已接入接口 · 未指定模型"
    # 兜底是否已挂上，同样属于「用户该知道、但界面上否则看不出来」的状态：
    # 不写出来，用户就无从判断一次查词失败到底是彻底失败还是已经兜住了。
    if config.get("offline_fallback", True):
        from .offline import resolve_offline_dict

        if resolve_offline_dict() is not None:
            return f"{base} · 失败时回落离线词库"
    return base


def build_provider(config: dict, api_key: str):
    """按配置构造 Provider。

    两条降级路径都优先落到**离线词库**而不是示例词条：只有当机器上连词库都没有
    时才退回 3 个演示词的 mock——一本真词典远比「演示效果」有用。

    第三条是**运行期**降级：接口已配好、但在查某个词的当下失败（超时、断网、
    额度用尽）。这类失败用户当场解决不了，却会让他连一个词都查不到，因此默认
    用 `FallbackProvider` 接住。它只在接口模式下生效——离线模式已经是兜底本身，
    再包一层没有意义。
    """
    mode = config.get("provider")
    if mode == "offline":
        from .offline import OfflineDictProvider

        return OfflineDictProvider()
    if mode == "mock":
        from .mock import MockProvider

        return MockProvider()
    if not api_key:
        from .offline import OfflineDictProvider, resolve_offline_dict

        if resolve_offline_dict() is not None:
            return OfflineDictProvider()
        from .mock import MockProvider

        return MockProvider()

    primary = OpenAICompatProvider(
        base_url=config.get("base_url", ""),
        api_key=api_key,
        model=config.get("model", ""),
        timeout=float(config.get("timeout", 30.0)),
    )

    if config.get("offline_fallback", True):
        from .offline import OfflineDictProvider, resolve_offline_dict

        # 先确认词库真的在，再构造——OfflineDictProvider 找不到文件时会抛
        # ProviderError，那会让「接口本来能用」变成「程序起不来」。
        if resolve_offline_dict() is not None:
            from .fallback import FallbackProvider

            return FallbackProvider(primary, OfflineDictProvider())
    return primary
