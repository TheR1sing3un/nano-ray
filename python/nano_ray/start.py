"""CLI entry point for starting nano-ray head or worker nodes.

Usage:
    # Start a head node (with optional local workers)
    python -m nano_ray.start --head --port 6379 --num-workers 2

    # Start a worker node connecting to head
    python -m nano_ray.start --address 192.168.1.100:6379 --num-workers 4

This mirrors Ray's CLI:
    ray start --head --port 6379
    ray start --address 192.168.1.100:6379
"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a nano-ray head or worker node.")
    parser.add_argument(
        "--head",
        action="store_true",
        help="Start as head node (cluster coordinator).",
    )
    parser.add_argument(
        "--address",
        type=str,
        default=None,
        help="Address of the head node to connect to (host:port).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6379,
        help="Port for the head node to listen on (default: 6379).",
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        default=4,
        help="Number of local worker processes (default: 4).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host for the head node to bind to (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8265,
        help="Port for the dashboard HTTP server (default: 8265, use 0 to disable).",
    )

    args = parser.parse_args()

    if args.head and args.address:
        parser.error("Cannot specify both --head and --address.")

    if not args.head and not args.address:
        parser.error("Must specify either --head or --address.")

    if args.head:
        from nano_ray.head import HeadService

        dashboard_port = args.dashboard_port if args.dashboard_port != 0 else None
        head = HeadService(
            host=args.host,
            port=args.port,
            num_workers=args.num_workers,
            dashboard_port=dashboard_port,
        )
        print(
            f"Starting nano-ray head node on {args.host}:{args.port} "
            f"with {args.num_workers} local workers"
        )
        if dashboard_port:
            print(f"Dashboard: http://127.0.0.1:{dashboard_port}")
        head.serve_forever()
    else:
        from nano_ray.node import WorkerNodeService

        node = WorkerNodeService(
            head_address=args.address,
            num_workers=args.num_workers,
        )
        node.serve_forever()


if __name__ == "__main__":
    main()
