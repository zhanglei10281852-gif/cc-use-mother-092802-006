"""启动限发事件与电量归因服务：python -m curtailment_case [--db PATH] [--port N]"""

from __future__ import annotations

import argparse

from .api import make_server
from .service import CurtailmentService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="curtailment_case", description="限发事件与电量归因服务（HTTP/JSON）"
    )
    parser.add_argument("--db", default="curtailment.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    service = CurtailmentService(args.db)
    httpd = make_server(service, args.host, args.port)
    print(f"限发争议服务已启动: http://{args.host}:{args.port} (db={args.db})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
