import csv
import logging
import random
from datetime import datetime
from io import StringIO
from typing import Any

import jwt
from django.core.mail import send_mail
from django.shortcuts import get_object_or_404
from ninja import File, Router, UploadedFile

from accounts.models import Account, Email
from megazord.api.requests import APIRequest
from megazord.schemas import ErrorSchema
from megazord.settings import EMAIL_HOST_USER, SECRET_KEY
from teams.models import Team, Token
from teams.schemas import TeamById

from .models import Hackathon
from .schemas import (
    AddUserToHack,
    EditHackathon,
    Error,
    HackathonIn,
    HackathonSchema,
    StatusOK,
)

logger = logging.getLogger(__name__)

hackathon_router = Router()
my_hackathon_router = Router()

INVITE_SUBJECT_TEMPLATE = "Приглашение в хакатон {hackathon_name}"
INVITE_BODY_TEMPLATE = """Вас пригласили на хакатон {hackathon_name} с помощью сервиса для упрощённого набора команд XaXack.
 Для принятия приглашения перейдите по ссылке: http://localhost:3000/join-hackaton?hackathon_id={invite_code}"""


@hackathon_router.post(
    path="/",
    response={201: HackathonSchema, 401: ErrorSchema, 400: ErrorSchema},
)
def create_hackathon(
    request: APIRequest, body: HackathonIn, image_cover: UploadedFile = File(...)
):
    user = request.user
    if not user.is_organizator:
        return 403, {
            "detail": "You are not organizator and you can't create hackathons"
        }

    body_dict = body.dict()
    hackathon = Hackathon(
        creator=user,
        name=body_dict["name"],
        description=body_dict["description"],
        min_participants=body.min_participants,
        max_participants=body.max_participants,
    )
    hackathon.save()
    hackathon.image_cover.save(image_cover.name, image_cover)

    for participant in body_dict["participants"]:
        try:
            participant_acc = Account.objects.get(email=participant)
        except Account.DoesNotExist:
            participant_acc = None

        # Создание или получение объекта Email
        email_obj, created = Email.objects.get_or_create(email=participant)

        # Добавление email в хакатон
        hackathon.emails.add(email_obj)

        # Генерация JWT
        encoded_jwt = jwt.encode(
            {
                "createdAt": datetime.utcnow().timestamp(),
                "id": hackathon.id,
                "email": participant,
            },
            SECRET_KEY,
            algorithm="HS256",
        )

        if participant_acc == hackathon.creator:
            continue

        try:
            Token.objects.create(token=encoded_jwt, is_active=True)
            logger.info(
                INVITE_BODY_TEMPLATE.format(
                    hackathon_name=hackathon.name, invite_code=encoded_jwt
                )
            )
            send_mail(
                subject=INVITE_SUBJECT_TEMPLATE.format(hackathon_name=hackathon.name),
                message=INVITE_BODY_TEMPLATE.format(
                    hackathon_name=hackathon.name, invite_code=encoded_jwt
                ),
                from_email="noreply@zotov.dev",
                recipient_list=[participant],
                fail_silently=False,
            )
        except Exception as e:
            logger.critical(e)

    return 201, hackathon


@hackathon_router.post(
    path="/join", response={403: Error, 200: HackathonSchema, 401: Error}
)
def join_hackathon(request: APIRequest, hackathon_id: int, token: str):
    user = request.user
    tkn = get_object_or_404(Token, token=token)
    if not tkn.is_active:
        return 403, {"details": "token in not active"}
    else:
        tkn.is_active = False
        tkn.save()
    hackathon = get_object_or_404(Hackathon, id=hackathon_id)
    hackathon.participants.add(user)
    hackathon.save()
    return 200, hackathon


@hackathon_router.get(path="/", response={401: Error, 200: list[HackathonSchema]})
def list_hackathons(request):
    hackathons = Hackathon.objects.all()
    return 200, hackathons


@hackathon_router.post(
    path="/{hackathon_id}/add_user",
    response={
        201: HackathonSchema,
        401: Error,
        404: Error,
        403: Error,
        400: Error,
        500: Error,
    },
)
def add_user_to_hackathon(
    request: APIRequest, hackathon_id: int, email_schema: AddUserToHack
):
    me = request.user
    hackathon = get_object_or_404(Hackathon, id=hackathon_id)

    if hackathon.creator != me:
        return 403, {"details": "You are not creator and you can't edit this hackathon"}

    # Проверка, является ли email адресом создателя хакатона
    if hackathon.creator.email == email_schema.email:
        return 400, {"details": "user is creator of the hackathon"}

    # Поиск или создание записи email
    email_obj, created = Email.objects.get_or_create(email=email_schema.email)

    # Добавление email в хакатон
    hackathon.emails.add(email_obj)
    hackathon.save()

    # Создание JWT токена для приглашения
    encoded_jwt = jwt.encode(
        {
            "createdAt": datetime.utcnow().timestamp(),
            "hackathon_id": hackathon.id,
            "email": email_schema.email,
        },
        SECRET_KEY,
        algorithm="HS256",
    )

    if email_schema.email == hackathon.creator.email:
        return 400, {"details": "user is creator of the hackathon"}

    try:
        Token.objects.create(token=encoded_jwt, is_active=True)
        send_mail(
            subject=INVITE_SUBJECT_TEMPLATE.format(hackathon_name=hackathon.name),
            message=INVITE_BODY_TEMPLATE.format(
                hackathon_name=hackathon.name, invite_code=encoded_jwt
            ),
            from_email=EMAIL_HOST_USER,
            recipient_list=[email_schema.email],
            fail_silently=False,
        )
    except Exception as e:
        return 500, {"details": f"Failed to send email: {str(e)}"}

    return 201, hackathon


@hackathon_router.delete(
    path="/{hackathon_id}/remove_user",
    response={201: HackathonSchema, 401: Error, 404: Error, 403: Error, 400: Error},
)
def remove_user_from_hackathon(
    request: APIRequest, hackathon_id: int, email_schema: AddUserToHack
):
    me = request.user
    hackathon = get_object_or_404(Hackathon, id=hackathon_id)
    user_to_remove = get_object_or_404(Account, email=email_schema.email)
    if hackathon.creator == me:
        if user_to_remove != hackathon.creator:
            if user_to_remove in hackathon.participants.all():
                hackathon.participants.remove(user_to_remove)
                hackathon.save()
            return 201, hackathon
        else:
            return 400, {"detail": "This user is creator of hackathon"}
    else:
        return 403, {"detail": "You are not creator and you can't edit this hackathon"}


@hackathon_router.patch(
    path="/{id}",
    response={200: HackathonSchema, 401: Error, 400: Error, 403: Error, 404: Error},
)
def edit_hackathons(request: APIRequest, id: int, body: EditHackathon):
    hackathon = get_object_or_404(Hackathon, id=id)
    user = request.user
    if hackathon.creator == user:
        if body.name:
            hackathon.name = body.name
            hackathon.save()
        if body.description:
            hackathon.description = body.description
            hackathon.save()
        if body.min_participants:
            hackathon.min_participants = body.min_participants
            hackathon.save()
        if body.max_participants:
            hackathon.max_participants = body.max_participants
            hackathon.save()
        return 200, hackathon
    else:
        return 403, {"detail": "You are not creator and you can't edit this hackathons"}


@hackathon_router.post(
    path="/{id}/change_photo",
    response={200: HackathonSchema, 401: Error, 400: Error, 403: Error, 404: Error},
)
def change_photo(request: APIRequest, id: int, image_cover: UploadedFile = File(...)):
    hackathon = get_object_or_404(Hackathon, id=id)
    user = request.user
    if hackathon.creator == user:
        if image_cover:
            hackathon.image_cover.save(image_cover.name, image_cover)
        return 200, hackathon
    else:
        return 403, {"detail": "You are not creator and you can't edit this hackathons"}


@hackathon_router.get(
    path="/{id}",
    response={200: HackathonSchema, 401: Error, 400: Error, 404: Error},
)
def get_specific_hackathon(request: APIRequest, id: int) -> tuple[int, Hackathon]:
    hackathon = get_object_or_404(Hackathon, id=id)
    return 200, hackathon


@my_hackathon_router.get(path="/", response={401: Error, 200: list[HackathonSchema]})
def list_myhackathons(request):
    user = request.user
    hackathons = Hackathon.objects.all()
    to_return = []
    for hack in hackathons:
        if hack.creator == user or user in hack.participants.all():
            to_return.append(hack)
    return 200, to_return


@hackathon_router.get(path="/get_user_team/{id}", response={200: TeamById, 404: Error})
def get_user_team_in_hackathon(request: APIRequest, id: str):
    user = request.user
    user_id = user.id
    hackathon = get_object_or_404(Hackathon, id=int(id))
    teams = Team.objects.filter(hackathon=hackathon).all()
    if teams:
        for t in teams:
            for member in t.team_members.all():
                if int(user_id) == int(member.id):
                    return 200, {
                        "id": t.id,
                        "hackathon": t.hackathon.id,
                        "name": t.name,
                        "creator": t.creator.id,
                        "team_members": [
                            {
                                "id": member.id,
                                "email": member.email,
                                "name": member.username,
                            }
                            for member in t.team_members.all()
                        ],
                    }
    else:
        return 404, {"details": "Not found"}


@hackathon_router.get(
    path="/{id}/load_txt", response={200: StatusOK, 403: Error, 404: Error}
)
def load_txt(request: APIRequest, id: str, file: UploadedFile = File(...)):
    me = request.user
    hackathon = get_object_or_404(Hackathon, id=int(id))
    if me == hackathon.creator:
        return {"details": "you have no access"}
    for n, i in enumerate(file):
        try:
            participant_acc = Account.objects.get(email=i)
        except:
            participant_acc = None
        encoded_jwt = jwt.encode(
            {
                "num": n,
                "createdAt": datetime.utcnow().timestamp(),
                "id": hackathon.id,
                "email": i,
            },
            SECRET_KEY,
            algorithm="HS256",
        )
        if participant_acc == hackathon.creator:
            continue
        try:
            Token.objects.create(token=encoded_jwt, is_active=True)
            logger.info(
                INVITE_BODY_TEMPLATE.format(
                    hackathon_name=hackathon.name, invite_code=encoded_jwt
                )
            )
            send_mail(
                subject=INVITE_SUBJECT_TEMPLATE.format(hackathon_name=hackathon.name),
                message=INVITE_BODY_TEMPLATE.format(
                    hackathon_name=hackathon.name, invite_code=encoded_jwt
                ),
                from_email="noreply@zotov.dev",
                recipient_list=[i],
                fail_silently=False,
            )
        except Exception as e:
            logger.critical(e)


@hackathon_router.post(
    path="/{hackathon_id}/send_code",
    response={200: Any, 400: Error, 404: Error, 403: Error},
)
def send_code_to_email(request, hackathon_id: int, email_schema: AddUserToHack):
    # Получение хакатона по id
    hackathon = get_object_or_404(Hackathon, id=hackathon_id)

    # Проверка, существует ли email в списке emails хакатона
    email_obj = get_object_or_404(Email, email=email_schema.email)

    if email_obj not in hackathon.emails.all():
        return {"details": "Email not found in hackathon"}, 404

    # Генерация 6-значного кода
    confirmation_code = str(random.randint(100000, 999999))

    # Создание JWT для хранения кода подтверждения
    encoded_jwt = jwt.encode(
        {
            "confirmation_code": confirmation_code,
            "email": email_schema.email,
            "createdAt": datetime.utcnow().timestamp(),
        },
        SECRET_KEY,
        algorithm="HS256",
    )

    # Сохранение токена в базе данных
    try:
        Token.objects.create(token=encoded_jwt, is_active=True)
    except Exception as e:
        logger.critical(e)
        return {"details": "Failed to create token"}, 500

    # Отправка email с кодом подтверждения
    try:
        send_mail(
            "Ваш код подтверждения",
            f"Ваш код подтверждения: {confirmation_code}",
            "noreply@zotov.dev",
            [email_schema.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.critical(e)
        return {"details": "Failed to send email"}, 500

    return {"details": "Confirmation code sent successfully"}, 200


@hackathon_router.post(
    path="/{hackathon_id}/upload_emails",
    response={200: StatusOK, 400: Error, 404: Error, 403: Error},
)
def upload_emails_to_hackathon(
    request: APIRequest, hackathon_id: int, csv_file: UploadedFile = File(...)
):
    user = request.user
    hackathon = get_object_or_404(Hackathon, id=hackathon_id)

    # Проверка, является ли пользователь создателем хакатона
    if hackathon.creator != user:
        return 403, {"details": "You are not the creator or cannot edit this hackathon"}

    try:
        # Чтение и парсинг CSV файла
        file_data = csv_file.read().decode(
            "utf-8-sig"
        )  # Используем 'utf-8-sig' для удаления BOM
        csv_reader = csv.reader(StringIO(file_data))

        for row in csv_reader:
            email = row[
                0
            ].strip()  # Предполагается, что email находится в первом столбце
            # Создание или получение объекта Email
            email_obj, created = Email.objects.get_or_create(email=email)

            # Добавление email в хакатон
            hackathon.emails.add(email_obj)

        hackathon.save()

    except Exception as e:
        logger.critical(f"Failed to process CSV file: {str(e)}")
        return 400, {"details": "Failed to process CSV file"}

    return 200, {"details": "Emails successfully uploaded"}
