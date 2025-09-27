from .helper import *
from json import dumps
from requests import Response

test = Test()

USE_FAKE_EXTERNAL_AUTH = not input("Use fake external auth? Enter if yes, anything if no ")

# TODO: support for multiple functions for the same path

@test.route("/accounts/session/external/id/", "POST")
def external_auth_step1():
    response: Response = yield
    yield response.json()["id"]

if USE_FAKE_EXTERNAL_AUTH:
    @test.route("/accounts/session/external/", "POST", dependencies=[After(external_auth_step1, value="external_auth_id")])
    def external_auth(external_auth_id: str):
        response: Response = yield Body(
            data={
                "id": "buuecq3cbgku8mjbl0ii1almkrt812zvg52bg4zfbek1kjiwyqtv32tx4wtzevws", # random
                "handle": "111111",
                "name": "test user",
                "avatarType": "default",
                "avatarForegroundType": "player",
                "avatarForegroundColor": "#ffffffff",
                "avatarBackgroundType": "default",
                "avatarBackgroundColor": "#000020ff",
                "bannerType": "none",
                "aboutMe": "hii",
                "favorites": [],

                "type": "external", 
                "id_key": external_auth_id
            }, 
            use_private_auth=True
        )
        yield response.json()["session"]
else:
    @test.route("/accounts/session/external/get/", "GET", dependencies=[After(external_auth_step1, value="external_auth_id")])
    def external_auth(external_auth_id: str):
        print(f"Auth at https://open.sonolus.com/external-login/{test.sonolus_url}?id={external_auth_id} and press enter to continue", end=" ")
        input()

        response: Response = yield Body(params={"id": external_auth_id})
        data = response.json()

        if not data:
            raise Exception("not authorized")
    
        yield data["session_key"]


@test.route("/accounts/session/account", "GET", dependencies=[After(external_auth, use_for_auth=True)])
def account():
    response: Response = yield
    yield response.json()

@test.route("/accounts/{id}/staff/admin", "PATCH", dependencies=[After(account, value="account")])
def admin(account: dict):
    yield Body(format_path={"id": account["sonolus_id"]}, use_private_auth=True)

@test.route("/accounts/{id}/staff/unadmin", "PATCH", dependencies=[After(account, value="account"), After(admin)])
def unadmin(account: dict):
    yield Body(format_path={"id": account["sonolus_id"]}, use_private_auth=True)

@test.route("/accounts/{id}/staff/mod", "PATCH", dependencies=[After(account, value="account")])
def mod(account: dict):
    yield Body(format_path={"id": account["sonolus_id"]}, use_private_auth=True)

@test.route("/accounts/notifications/", "POST", dependencies=[After(external_auth, use_for_auth=True), After(account, value="account"), After(mod)])
def add_notification(account: dict):
    yield Body(data={
        "user_id": account["sonolus_id"],
        "title": "notification.",
        "content": "test"
    })

@test.route("/accounts/notifications/", "GET", dependencies=[After(external_auth, use_for_auth=True), After(add_notification)])
def get_notification_list():
    response: Response = yield

    data = response.json()
    if not data["notifications"]:
        raise Exception("no notifications")
    
    yield data["notifications"][0]["id"]

@test.route("/accounts/notifications/{id}/", "GET", dependencies=[After(external_auth, use_for_auth=True), After(get_notification_list, value="id")])
def get_notification(id: int):
    yield Body(format_path={"id": str(id)})

@test.route("/accounts/notifications/{id}/", "DELETE", dependencies=[After(external_auth, use_for_auth=True), After(get_notification_list, value="id")])
def delete_notification(id: int):
    yield Body(format_path={"id": str(id)})

@test.route("/charts/", "GET")
def charts():
    yield

@test.route("/charts/upload/", "POST", dependencies=[After(external_auth, use_for_auth=True)])
def upload_chart():
    response: Response = yield Body(
        form_data={
            "data": dumps({
                "rating": 10,
                "title": "Cool Level",
                "author": "Cool Author",
                "artists": "Cool Artist",
                "tags": ["test", "test2"],
                "includes_background": False,
                "includes_preview": False,
            })
        },
        files={
            "jacket_image": ("jacket.jpg", open("assets/jacket.jpg", "rb"), "image/jpeg"),
            "chart_file": ("chart.sus", open("assets/chart.usc", "r"), "application/json"),
            "audio_file": ("audio.mp3", open("assets/music.mp3", "rb"), "audio/mpeg"),
        }
    )

    yield response.json()["id"]

@test.route("/charts/{id}/edit/", "PATCH", dependencies=[After(external_auth, use_for_auth=True), After(upload_chart, value="id")])
def edit_chart(id: str):
    yield Body(form_data={"data": dumps({"rating": 15})}, format_path={"id": id})

@test.route("/charts/{id}/", "GET", dependencies=[After(external_auth, use_for_auth=True), After(upload_chart, value="id")])
def get_chart(id: str):
    response: Response = yield Body(format_path={"id": id})

    if response.json()["data"]["rating"] != 15 and test.check(edit_chart):
        print("rating remains unedited")
        raise SkipRoute

@test.route("/charts/{id}/like/", "POST", dependencies=[After(external_auth, use_for_auth=True), After(upload_chart, value="id")])
def like_chart(id: str):
    yield Body(data={"type": "like"}, format_path={"id": id})

@test.route("/charts/{id}/visibility/", "PATCH", dependencies=[After(external_auth, use_for_auth=True), After(upload_chart, value="id")])
def change_chart_visibility(id: str):
    yield Body(data={"status": "PUBLIC"}, format_path={"id": id})

@test.route("/accounts/session/", "POST", dependencies=[After(account, value="account")])
def game_auth(account: dict):
    response: Response = yield Body(
        data={
            "type": "game",

            "id": account["sonolus_id"],
            "handle": str(account["sonolus_handle"]),
            "name": account["sonolus_username"],
            "avatarType": "default",
            "avatarForegroundType": "player",
            "avatarForegroundColor": "#ffffffff",
            "avatarBackgroundType": "default",
            "avatarBackgroundColor": "#000020ff",
            "bannerType": "none",
            "aboutMe": "hii",
            "favorites": [],
        },
        use_private_auth=True
    )

    yield response.json()["session"]

@test.route("/charts/{id}/stpick", "PATCH", dependencies=[After(game_auth, use_for_auth=True), After(upload_chart, value="id")])
def staff_pick_chart(id: str):
    yield Body(data={"value": True}, format_path={"id": id})

@test.route("/accounts/{id}/staff/", "PATCH", dependencies=[After(account, value="account"), After(mod)])
def unmod(account: dict):
    yield Body(format_path={"id": account["sonolus_id"]}, use_private_auth=True)

@test.route("/charts/{id}/comment", "POST", dependencies=[After(game_auth, use_for_auth=True), After(upload_chart, value="id")])
def add_comment(id: str):
    yield Body(data={"content": "test comment"}, format_path={"id": id})

@test.route("/charts/{id}/comment/", "GET", dependencies=[After(game_auth, use_for_auth=True), After(upload_chart, value="id"), After(add_comment)])
def get_comments(id: str):
    response: Response = yield Body(format_path={"id": id})
    yield response.json()["data"][0]["id"]

@test.route("/charts/{chart_id}/comment/{comment_id}/", "DELETE", dependencies=[After(game_auth, use_for_auth=True), After(upload_chart, value="chart_id"), After(get_comments, value="comment_id")])
def delete_comment(chart_id: str, comment_id: int):
    yield Body(format_path={"chart_id": chart_id, "comment_id": str(comment_id)})

@test.route("/charts/{id}/delete/", "DELETE", dependencies=[After(game_auth, use_for_auth=True), After(upload_chart, value="id")])
def delete_chart(id: str):
    yield Body(format_path={"id": id})

test.start()