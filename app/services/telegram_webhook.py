from __future__ import annotations
from datetime import datetime
from sqlalchemy import select
from app.db.database import session_scope
from app.core.permissions import has_access
from app.db.models import User,PhotoRequest,Photo,ShiftReport,ShiftReportValue,Inspection,InspectionValue,Task,CashCollection,PriceListUploadRequest,PriceList,TaskAttachmentRequest,TaskAttachment,TaskAssignee,KnowledgeMediaUploadRequest,KnowledgeMedia,TrainingQuestionMediaRequest,TrainingQuestion,TelegramDestination
from app.services.telegram import send_message,miniapp_keyboard
from app.services.storage import mirror_telegram_photo_to_s3


def handle_update(update:dict):
    message=update.get("message") or {}
    if message.get("photo"):
        send_message(message["chat"]["id"],"Фото теперь добавляются прямо в Mini App: откройте нужный раздел и нажмите «Добавить фото».")
        return
    msg=update.get("message") or {}
    chat=msg.get("chat") or {}
    tg_user=msg.get("from") or {}
    if not chat.get("id") or not tg_user.get("id"):return
    tid=int(tg_user["id"]);text=(msg.get("text") or "").strip()
    if text.split("@")[0]=="/register" and chat.get("type") in {"group","supergroup","channel"}:
        with session_scope() as db:
            user=db.scalar(select(User).where(User.telegram_id==tid,User.active.is_(True)))
            if not user or not has_access(db,user,"telegram.groups","edit"):
                send_message(int(chat["id"]),"⛔ У вас нет права регистрировать Telegram-группы в Причал Core.")
                return
            dest=db.scalar(select(TelegramDestination).where(TelegramDestination.chat_id==int(chat["id"])))
            title=chat.get("title") or chat.get("username") or f"Telegram {chat['id']}"
            if not dest:
                dest=TelegramDestination(chat_id=int(chat["id"]),chat_type=chat.get("type") or "group",title=title,username=chat.get("username"),active=True,registered_by_user_id=user.id)
                db.add(dest);db.flush()
            else:
                dest.title=title;dest.username=chat.get("username");dest.chat_type=chat.get("type") or dest.chat_type;dest.active=True;dest.registered_by_user_id=user.id;dest.last_seen_at=datetime.utcnow()
            send_message(int(chat["id"]),"✅ <b>Группа зарегистрирована в Причал Core</b>\nТеперь откройте Core → Ещё → Telegram → Маршруты и привяжите группу к магазину или поставщику.")
        return
    if text=="/start":
        send_message(tid,"⚓ <b>Причал Core</b>\nЕдиный рабочий центр сети.",miniapp_keyboard());return
    if text=="/whoami":
        send_message(tid,f"Ваш Telegram ID: <code>{tid}</code>");return

    document=msg.get("document")
    if document:
        with session_scope() as db:
            user=db.scalar(select(User).where(User.telegram_id==tid))
            if not user:
                send_message(tid,"Сначала откройте MiniApp и авторизуйтесь.");return
            task_req=db.scalar(select(TaskAttachmentRequest).where(TaskAttachmentRequest.user_id==user.id,TaskAttachmentRequest.kind=="file",TaskAttachmentRequest.status=="waiting",TaskAttachmentRequest.expires_at>datetime.utcnow()).order_by(TaskAttachmentRequest.created_at.desc()))
            if task_req:
                db.add(TaskAttachment(task_id=task_req.task_id,assignee_id=task_req.assignee_id,checklist_item_id=task_req.checklist_item_id,uploaded_by=user.id,kind="file",telegram_file_id=document["file_id"],telegram_file_unique_id=document.get("file_unique_id"),telegram_chat_id=chat.get("id"),telegram_message_id=msg.get("message_id"),file_name=document.get("file_name"),mime_type=document.get("mime_type"),file_size=document.get("file_size"),label=task_req.label))
                task_req.status="done"
                send_message(tid,"✅ Файл прикреплён к задаче.")
                return
            req=db.scalar(select(PriceListUploadRequest).where(PriceListUploadRequest.user_id==user.id,PriceListUploadRequest.status=="waiting",PriceListUploadRequest.expires_at>datetime.utcnow()).order_by(PriceListUploadRequest.created_at.desc()))
            if not req:
                send_message(tid,"Сейчас Core не ожидает файл. Сначала откройте нужное действие в MiniApp.");return
            db.add(PriceList(
                supplier_id=req.supplier_id,
                title=req.title or document.get("file_name") or "Прайс-лист",
                uploaded_by=user.id,
                telegram_file_id=document["file_id"],
                telegram_file_unique_id=document.get("file_unique_id"),
                telegram_chat_id=chat.get("id"),
                telegram_message_id=msg.get("message_id"),
                file_name=document.get("file_name"),
                mime_type=document.get("mime_type"),
                file_size=document.get("file_size"),
                active=True,
            ))
            req.status="done"
            send_message(tid,"✅ Прайс-лист прикреплён к поставщику.")
        return
    video=msg.get("video")
    if video:
        with session_scope() as db:
            user=db.scalar(select(User).where(User.telegram_id==tid))
            if not user:
                send_message(tid,"Сначала откройте MiniApp и авторизуйтесь.");return
            kb_req=db.scalar(select(KnowledgeMediaUploadRequest).where(KnowledgeMediaUploadRequest.user_id==user.id,KnowledgeMediaUploadRequest.kind=="video",KnowledgeMediaUploadRequest.status=="waiting",KnowledgeMediaUploadRequest.expires_at>datetime.utcnow()).order_by(KnowledgeMediaUploadRequest.created_at.desc()))
            if not kb_req:
                send_message(tid,"Сейчас Core не ожидает видео. Сначала нажмите «Добавить видео» в статье базы знаний.");return
            db.add(KnowledgeMedia(article_id=kb_req.article_id,uploaded_by=user.id,kind="video",telegram_file_id=video["file_id"],telegram_file_unique_id=video.get("file_unique_id"),telegram_chat_id=chat.get("id"),telegram_message_id=msg.get("message_id"),file_name=video.get("file_name"),mime_type=video.get("mime_type") or "video/mp4",file_size=video.get("file_size"),caption=msg.get("caption")))
            kb_req.status="done"
            send_message(tid,"✅ Видео добавлено в базу знаний. Вернитесь в статью, чтобы вставить его в текст.")
        return
