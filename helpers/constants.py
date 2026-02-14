MAX_FILE_SIZES = {
    "jacket": int(7.5 * 1024 * 1024),  # 7.5 MB
    "chart": 20 * 1024 * 1024,  # 20 MB
    "audio": 50 * 1024 * 1024,  # 50 MB
    "preview": 5 * 1024 * 1024,  # 5 MB
    "background": 15 * 1024 * 1024,  # 15 MB
    "account_pfp": 20 * 1024 * 1024,  # 15MB
    "account_banner": 40 * 1024 * 1024,  # 20MB
}

MAX_TEXT_SIZES = {
    "description": 1000,
    "artists": 50,
    "author": 50,
    "title": 50,
    "per_tag": 10,
    "tags_count": 3,
}

MAX_RATINGS = {"min": -999, "max": 999, "decimal_places": 4}
