import threading

import mock
import pytest
import requests
import tornado.httpserver
import tornado.ioloop
import tornado.web

from opentracing_instrumentation.client_hooks.requests import patcher
from opentracing_instrumentation.request_context import span_in_context


@pytest.fixture(name='response_handler_hook')
def patch_requests(hook):
    if hook:
        # using regular method instead of mock.Mock() to be sure
        # that it works as expected with Python 2.7
        def response_handler_hook(response, span):
            response_handler_hook.called_with = response, span
        response_handler_hook.called_with = None
    else:
        response_handler_hook = None

    patcher.install_patches()
    patcher.set_response_handler_hook(response_handler_hook)
    try:
        yield response_handler_hook
    finally:
        patcher.reset_patches()


@pytest.fixture
def tornado_url(request, base_url, _unused_port):

    class Handler(tornado.web.RequestHandler):
        def get(self):
            self.write(self.request.headers['ot-tracer-traceid'])
            app.headers = self.request.headers

    app = tornado.web.Application([('/', Handler)])

    def run_http_server():
        io_loop = tornado.ioloop.IOLoop.current()
        http_server = tornado.httpserver.HTTPServer(app)
        http_server.add_socket(_unused_port[0])

        def stop():
            http_server.stop()
            io_loop.stop()
            thread.join()

        # finalizer should be added before starting of the IO loop
        request.addfinalizer(stop)

        io_loop.start()

    # running an http server in a separate thread in purpose
    # to make it accessible for the requests from the current thread
    thread = threading.Thread(target=run_http_server)
    thread.start()

    return base_url + '/'


def _test_requests(url, root_span, tracer, response_handler_hook):
    if root_span:
        root_span = tracer.start_span('root-span')
    else:
        root_span = None

    with span_in_context(span=root_span):
        response = requests.get(url)

    assert len(tracer.recorder.get_spans()) == 1

    span = tracer.recorder.get_spans()[0]
    assert span.tags.get('span.kind') == 'client'
    assert span.tags.get('http.url') == url

    # verify trace-id was correctly injected into headers
    trace_id = '%x' % span.context.trace_id
    assert response.text == trace_id

    if response_handler_hook:
        assert response_handler_hook.called_with == (response, span)


@pytest.mark.parametrize('scheme', ('http', 'https'))
@pytest.mark.parametrize('root_span', (True, False))
@pytest.mark.parametrize('hook', (True, False))
@mock.patch('requests.adapters.HTTPAdapter.cert_verify')
@mock.patch('requests.adapters.HTTPAdapter.get_connection')
def test_requests_with_mock(get_connection_mock, cert_verify_mock,
                            scheme, root_span, tracer,
                            response_handler_hook):

    def urlopen(headers, **kwargs):
        stream_mock = mock.MagicMock(
            return_value=[headers['ot-tracer-traceid'].encode()]
        )
        return mock.MagicMock(stream=stream_mock)

    get_connection_mock.return_value.urlopen = urlopen
    url = scheme + '://example.com/'
    _test_requests(url, root_span, tracer, response_handler_hook)


@pytest.mark.parametrize('root_span', (True, False))
@pytest.mark.parametrize('hook', (True, False))
def test_requests_with_tornado(tornado_url, root_span, tracer,
                               response_handler_hook):
    _test_requests(tornado_url, root_span, tracer, response_handler_hook)
