"""One provider, one retry owner (the pipeline), bounded requests, no fallback."""
import json
import os
import http.client
from urllib import request, error
from .ark_schema import to_strict_schema

class ProviderError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable

class ArkMini:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.usage = []

    def complete(self, system, payload, schema, node):
        key = os.environ.get(self.config['api_key_env'])
        if not key:
            raise ProviderError('ARK_API_KEY 未配置；未发起网络请求', retryable=False)
        if self.config.get('response_format') != 'json_schema' or self.config.get('strict_schema') is not True:
            raise ProviderError('strict server-side JSON Schema is required', retryable=False)
        wire_schema = to_strict_schema(schema)
        body = {'model': self.config['model'], 'temperature': self.config['temperature'],
                'max_tokens': self.config['max_tokens'], 'thinking': self.config['thinking'],
                'response_format': {'type': 'json_schema','json_schema':{
                    'name': 'english_' + node, 'strict': True, 'schema': wire_schema}},
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}
        req = request.Request(self.config['base_url'] + '/chat/completions', data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                              headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        self.calls += 1
        try:
            with request.urlopen(req, timeout=self.config['timeout_seconds']) as response:
                data = json.load(response)
        except error.HTTPError as exc:
            # Do not log provider response bodies, credentials or request headers.
            raise ProviderError(f'Ark HTTP {exc.code}', exc.code in (408, 429) or exc.code >= 500) from None
        except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(type(exc).__name__) from None
        self.usage.append({'node': node, 'request_id': data.get('id'), 'usage': data.get('usage', {})})
        try:
            choice = data['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ProviderError('模型回答未完整结束')
            return json.loads(choice['message']['content'])
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise ProviderError('模型响应不是完整JSON') from None
