import argparse
import asyncio

from app.database import charts as charts_db
from app.database import notifications as notifications_db
from app.services.chart_access import CHART_ID_PATTERN
from scripts._common import (
    add_config_argument,
    add_constants_argument,
    connect,
    find_account,
    read_config,
    read_constants,
)


async def run(args: argparse.Namespace) -> None:
    """Send a notification to an account or a chart's owner.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    constants = read_constants(args.constants)
    limits = constants.text.max_lengths
    if len(args.title) > limits["notification_title"]:
        raise SystemExit(f"title is longer than {limits['notification_title']} characters")
    if args.content is not None and len(args.content) > limits["notification_content"]:
        raise SystemExit(f"content is longer than {limits['notification_content']} characters")

    connection = await connect(config.database)
    try:
        if args.chart_id is not None:
            chart_id = args.chart_id.removeprefix(config.sonolus.chart_prefix)
            if not CHART_ID_PATTERN.match(chart_id):
                raise SystemExit(f"not a chart id: {args.chart_id}")
            chart = await charts_db.get_chart(connection, chart_id)
            if chart is None:
                print(f"No chart found with id {chart_id}.")
                return
            user_id = chart.author
        else:
            account = await find_account(connection, args.id, args.handle)
            if account is None:
                if args.handle is not None:
                    print(f"No account found for handle #{args.handle}.")
                else:
                    print(f"No account found for sonolus_id {args.id}.")
                return
            user_id = account.sonolus_id

        row = await notifications_db.create_notification(
            connection, user_id, args.title, args.content
        )
        if row is None:
            print(f"No account found for sonolus_id {user_id}.")
            return

        print(f"Sent notification #{row.id} to {user_id}.")
    finally:
        await connection.close()


def main() -> None:
    """Parse the command line and send the notification."""
    p = argparse.ArgumentParser(description="Send a custom notification to a user")
    add_config_argument(p)
    add_constants_argument(p)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="Sonolus ID of the recipient")
    target.add_argument("--handle", type=int, help="Sonolus handle of the recipient")
    target.add_argument("--chart-id", help="send to the owner of this chart")
    p.add_argument("--title", required=True, help="Notification title")
    p.add_argument("--content", default=None, help="Notification body")
    args = p.parse_args()
    if not args.title.strip():
        p.error("--title must not be empty")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
