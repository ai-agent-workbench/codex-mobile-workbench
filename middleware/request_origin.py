"""Fail closed before body parsing, handler side effects or WS acceptance."""
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Match

from shared.request_origin import require_same_https_origin
from shared.web_push import WebPushError


class RequestOriginMiddleware:
    def __init__(self, app, *, router):
        self.app = app
        self.router = router

    def _retired(self, scope):
        # Exempt the actual tombstone handler, never a URL prefix. Future live
        # routes ahead of the catch-all still receive the normal guard.
        from routes.legacy_employee import retired_employee_portal
        def selected(routes):
            for route in routes:
                match, child = route.matches(scope)
                if match is not Match.FULL:
                    continue
                # New FastAPI lazily includes routers; its matches() does not
                # return the endpoint. Our included routers have no prefix.
                # An unfamiliar/prefixed wrapper fails closed, not exempt.
                nested = getattr(route, 'original_router', None)
                if nested is not None:
                    context = getattr(route, 'include_context', None)
                    return None if getattr(context, 'prefix', '') else selected(nested.routes)
                return child.get('endpoint')
            return None
        return selected(self.router.routes) is retired_employee_portal

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        unsafe = kind == 'http' and scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        if kind == 'websocket' or (unsafe and not self._retired(scope)):
            try:
                require_same_https_origin(HTTPConnection(scope), allow_form_referer=kind == 'http')
            except (WebPushError, ValueError):
                if kind == 'websocket':
                    await send({'type': 'websocket.close', 'code': 4403})
                else:
                    await JSONResponse(
                        {'error': '请求来源不匹配，请从工作台页面重试。', 'reason_code': 'origin_rejected'},
                        status_code=403, headers={'Cache-Control': 'no-store'},
                    )(scope, receive, send)
                return
        # /quick creates a draft on GET. Permit home links and explicit address
        # bar/PWA launches, not same-site navigation from another HTTPS port.
        if kind == 'http' and scope['method'] in {'GET', 'HEAD'} and scope['path'] == '/quick':
            connection = HTTPConnection(scope)
            if connection.headers.get('sec-fetch-site') not in {'same-origin', 'none'}:
                try:
                    require_same_https_origin(connection, allow_navigation_referer=True)
                except (WebPushError, ValueError):
                    await JSONResponse({'error': '请从工作台首页新建对话。', 'reason_code': 'origin_rejected'},
                                       status_code=403, headers={'Cache-Control': 'no-store'})(scope, receive, send)
                    return
        await self.app(scope, receive, send)
