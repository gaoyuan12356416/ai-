"""The public report process only handles reads. The existing X reservation broker stays independent."""
from http.server import ThreadingHTTPServer
from features.x_material_effects.api import Handler


class ReportHandler(Handler):
    def do_POST(self):
        self.send_json(405,dict(message='报表仅支持查询'))


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1',18989),ReportHandler).serve_forever()
