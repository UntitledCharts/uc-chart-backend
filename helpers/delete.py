import asyncio
from core import ChartFastAPI
from .models import Prefix
from database import leaderboards


async def delete_chart_files_from_s3(app: ChartFastAPI, author_id: str, chart_id: str):
    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(app.s3_bucket)
        batch = []
        async for obj in bucket.objects.filter(Prefix=f"{author_id}/{chart_id}/"):
            batch.append({"Key": obj.key})
            if len(batch) == 1000:
                await bucket.delete_objects(Delete={"Objects": batch})
                batch = []
        if batch:
            await bucket.delete_objects(Delete={"Objects": batch})


async def delete_from_s3(app: ChartFastAPI, account_id: str):
    bucket_name = app.s3_bucket

    prefixes: list[Prefix] = []

    async with app.db_acquire() as conn:
        prefixes.extend(
            await conn.fetch(leaderboards.get_leaderboard_prefix_for_user(account_id))
        )

    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(bucket_name)

        delete_batches = []

        # Delete everything under {id}/
        batch = []
        async for obj in bucket.objects.filter(Prefix=f"{account_id}/"):
            batch.append({"Key": obj.key})
            if len(batch) == 1000:
                delete_batches.append(batch)
                batch = []
        if batch:
            delete_batches.append(batch)

        # Delete everything under {prefix}/replays/{id}/
        for prefix in prefixes:
            full_prefix = f"{prefix.prefix}/replays/{account_id}/"
            batch = []
            async for obj in bucket.objects.filter(Prefix=full_prefix):
                batch.append({"Key": obj.key})
                if len(batch) == 1000:
                    delete_batches.append(batch)
                    batch = []
            if batch:
                delete_batches.append(batch)

        tasks = [
            bucket.delete_objects(Delete={"Objects": delete_batch})
            for delete_batch in delete_batches
        ]
        await asyncio.gather(*tasks)
