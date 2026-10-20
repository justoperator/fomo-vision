from django.urls import path
from . import views

urlpatterns = [
    path("projects/", views.projects_list, name="projects"),
    path("projects/create/", views.project_create, name="project_create"),
    path("join/", views.join_by_code, name="join_code"),
    path("join/<str:code>/", views.join_by_code, name="join"),
    path("connect/<str:code>/", views.camera_page, name="camera"),
    path("qr/<str:code>.png", views.qr_png, name="qr"),
    path("profile/", views.profile, name="profile"),
    path("p/<int:pid>/", views.workspace, name="workspace"),
    path("p/<int:pid>/delete/", views.project_delete, name="project_delete"),
    path("p/<int:pid>/leave/", views.project_leave, name="project_leave"),
    path("p/<int:pid>/api/state/", views.state_json, name="state"),
    path("p/<int:pid>/api/models/upload/", views.model_upload, name="model_upload"),
    path("p/<int:pid>/api/models/<int:mid>/update/", views.model_update, name="model_update"),
    path("p/<int:pid>/api/models/<int:mid>/delete/", views.model_delete, name="model_delete"),
    path("p/<int:pid>/api/invites/create/", views.invite_create, name="invite_create"),
    path("p/<int:pid>/api/invites/<int:iid>/revoke/", views.invite_revoke, name="invite_revoke"),
    path("p/<int:pid>/api/members/<int:uid>/update/", views.member_update, name="member_update"),
    path("p/<int:pid>/api/members/<int:uid>/remove/", views.member_remove, name="member_remove"),
]
