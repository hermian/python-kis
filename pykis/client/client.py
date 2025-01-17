TRACE_DETAIL_ERROR: bool = False
'''경고. 해당 기능은 HTTPStatusCode 200이 아닌 경우. 상세한 요청, 응답을 출력합니다.

이로 인해 예외 메세지에서 앱 키가 노출될 수 있습니다.'''

from datetime import datetime
from threading import Lock
import time
from typing import Literal, TypeVar
import requests

from .page import KisPage
from ..logging import KisLoggable
from .appkey import KisKey
from .token import KisAccessToken
from .responses import *

REAL_DOMAIN = 'https://openapi.koreainvestment.com:9443'
VIRTUAL_DOMAIN = 'https://openapivts.koreainvestment.com:29443'
API_REQUEST_PER_SECOND = 20 // 2

TRESPONSE = TypeVar('TRESPONSE', bound='KisResponse')

class KisClient(KisLoggable):
    '''한국투자증권 API 클라이언트'''
    key: KisKey
    '''한국투자증권 API Key'''
    token: KisAccessToken | None
    '''토큰'''
    session: requests.Session
    '''API 요청 세션'''

    _limit_lock: Lock
    _sf_time: float
    _sf_count: int = 0
    _verbose: bool = True

    def __init__(self, appkey: KisKey):
        self.key = appkey
        self.session = requests.Session()
        self.token = None
        self._limit_lock = Lock()
        self._sf_time = datetime.now().timestamp()

    def _wait_limit(self):
        '''API 요청 제한을 기다립니다.'''
        with self._limit_lock:
            now = datetime.now().timestamp()
            diff = now - self._sf_time
            
            if diff >= 1:
                self._sf_count = 0
                self._sf_time = now
            elif self._sf_count >= API_REQUEST_PER_SECOND:
                if self._verbose:
                    self.logger.warning('API rate limit exceeded. waiting...')

                time.sleep(1 - diff + 0.1)
        
                self._sf_count = 0
                self._sf_time = datetime.now().timestamp()
            
            self._sf_count += 1

    def build_url(self, path: str):
        '''API 요청 URL을 생성합니다.'''
        domain = VIRTUAL_DOMAIN if self.key.virtual_account else REAL_DOMAIN
        return f'{domain}{path}'


    def build_headers(self, auth: bool = True, page: bool = False) -> dict:
        '''API 요청 헤더를 생성합니다.'''
        if auth:
            self.ensure_token()

        header = {
            'appkey': self.key.appkey,
            'appsecret': self.key.appsecret
        }

        if auth:
            if not self.token:
                raise ValueError('Token이 없습니다.')

            header['authorization'] = f'{self.token.token_type} {self.token.token}'

        if page:
            header['tr_cont'] = 'N'

        return header

    def build_request(
        self,
        method: Literal['get', 'post'], 
        path: str,
        headers: dict = {},
        body: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
        page: KisPage | None = None,
        appkey_location: Literal['header', 'body'] = 'header',
    ):
        '''API 요청을 생성합니다.'''            
        if appkey_location == 'body' and method != 'post':
            raise ValueError('AppKey를 Body에 넣을 수 있는 메서드는 POST 뿐입니다.')
        
        if body != None and method != 'post':
            raise ValueError('Body를 넣을 수 있는 메서드는 POST 뿐입니다.')

        if appkey_location == 'header':
            headers.update(self.build_headers(auth, not page.empty if page else False))
        else:
            body = body or {}
            body.update(self.build_headers(auth, not page.empty if page else False))

        if body:
            for k, v in body.items():
                if not isinstance(v, str):
                    body[k] = str(v)

        url = self.build_url(path)

        if headers and 'tr_id' in headers and (params or body):
            self.logger.debug('API: %s %s, TR_ID: %s, PARAMES: %s, BODY: %s', method.upper(), path, headers['tr_id'], params, body)
        
        return requests.Request(
            method=method,
            url=url,
            headers=headers,
            json=body,
            params=params
        )

    def load_response(self, response: requests.Response, tResponse: type[TRESPONSE]) -> TRESPONSE:
        '''API 응답을 로드합니다.'''
        if response.status_code == 200:
            data = response.json()
            return tResponse(data, response)
        else:
            header = dict(response.request.headers)
            if 'appkey' in header:
                header['appkey'] = '***'
            if 'appsecret' in header:
                header['appsecret'] = '***'
            if 'authorization' in header:
                header['authorization'] = f'{header["authorization"].split()[0]} ***'
            if response.request.body:
                if isinstance(response.request.body, bytes):
                    body = response.request.body.decode('utf-8')
                else: body = response.request.body

                if not TRACE_DETAIL_ERROR and ('appkey' in body or 'appsecret' in body):
                    body = '[PROTECTED BODY]'
            else:
                body = '[EMPTY BODY]'

            raise ValueError(f'API 요청에 실패했습니다. ({response.status_code}) {response.text}\n[  Request  ]: {response.request.method} {response.request.url}\nHeaders: {header}\nBody: {body}')

    def request(
        self,
        method: Literal['get', 'post'], 
        path: str,
        headers: dict = {},
        body: dict | None = None,
        params: dict | None = None,
        auth: bool = True,
        page: KisPage | None = None,
        appkey_location: Literal['header', 'body'] = 'header',
        response: type[TRESPONSE] | None = None
    ) -> TRESPONSE:
        '''API 요청을 보냅니다.'''
        request = self.build_request(
            method,
            path,
            headers,
            body,
            params,
            auth,
            page,
            appkey_location
        )
        
        self._wait_limit()
        res = self.session.send(request.prepare())

        if response:
            return self.load_response(res, response)
        else:
            return KisResponse({}, res)  # type: ignore

    def ensure_token(self):
        '''토큰이 없으면 토큰을 발급합니다.'''
        if not self.token:
            self.token = KisAccessToken(self)
        
        self.token.ensure()

    
    def hashkey(
        self: 'KisClient',
        body: dict[str, str],
    ):
        '''해쉬키를 생성합니다. Body 암호화에 사용됩니다.'''
        return self.request(
            'post',
            '/uapi/hashkey',
            body=body,
            response=KisHashKeyResponse
        ).hash_key

    
    def ws_approvalkey(self: 'KisClient'):
        '''실시간 (웹소켓) 접속키 발급.'''
        return self.request(
            'post',
            '/oauth2/Approval',
            body={
                'grant_type': 'client_credentials',
                # 다른데는 다 appsecret인데 여기만 secretkey로 되어있음. 검수좀 제대로 하자..
                'appkey': self.key.appkey,
                'secretkey': self.key.appsecret,
            },
            appkey_location='body',
            response=KisWSApprovalKeyResponse
        ).approval_key
