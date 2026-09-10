from __future__ import annotations
from datetime import datetime
from sqlalchemy import select
from app.db.database import session_scope
from app.db.models import User,PhotoRequest,Photo,ShiftReport,ShiftReportValue,Inspection,InspectionValue,Task,CashCollection,PriceListUploadRequest,PriceList,TaskAttachmentRequest,TaskAttachment,TaskAssignee
from app.services.telegram import send_message,miniapp_keyboard


def handle_update(update:dict):
    msg=update.get("message") or {}
    chat=msg.get("chat") or {}
    tg_user=msg.get("from") or {}
    if not chat.get("id") or not tg_user.get("id"):return
    tid=int(tg_user["id"]);text=(msg.get("text") or "").strip()
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
    photos=msg.get("photo") or []
    if photos:
        with session_scope() as db:
            user=db.scalar(select(User).where(User.telegram_id==tid))
            if not user:
                send_message(tid,"Сначала откройте MiniApp и авторизуйтесь.");return
            task_req=db.scalar(select(TaskAttachmentRequest).where(TaskAttachmentRequest.user_id==user.id,TaskAttachmentRequest.kind=="photo",TaskAttachmentRequest.status=="waiting",TaskAttachmentRequest.expires_at>datetime.utcnow()).order_by(TaskAttachmentRequest.created_at.desc()))
            if task_req:
                p=photos[-1]
                db.add(TaskAttachment(task_id=task_req.task_id,assignee_id=task_req.assignee_id,checklist_item_id=task_req.checklist_item_id,uploaded_by=user.id,kind="photo",telegram_file_id=p["file_id"],telegram_file_unique_id=p.get("file_unique_id"),telegram_chat_id=chat.get("id"),telegram_message_id=msg.get("message_id"),file_name=None,mime_type="image/jpeg",file_size=p.get("file_size"),label=task_req.label))
                task_req.status="done"
                send_message(tid,"✅ Фото прикреплено к задаче.")
                return
            req=db.scalar(select(PhotoRequest).where(PhotoRequest.user_id==user.id,PhotoRequest.status=="waiting",PhotoRequest.expires_at>datetime.utcnow()).order_by(PhotoRequest.created_at.desc()))
            if not req:
                send_message(tid,"Сейчас Core не ожидает фотографию. Сначала нажмите «Добавить фото» в MiniApp.");return
            p=photos[-1]
            store_id=None
            model_map={"shift_report":ShiftReport,"inspection":Inspection,"task":Task,"cash_collection":CashCollection}
            model=model_map.get(req.entity_type)
            if model:
                entity=db.get(model,req.entity_id)
                store_id=getattr(entity,"store_id",None) if entity else None
            elif req.entity_type=="shift_report_field":
                value=db.get(ShiftReportValue,req.entity_id)
                report=db.get(ShiftReport,value.report_id) if value else None
                store_id=report.store_id if report else None
            elif req.entity_type=="inspection_value":
                value=db.get(InspectionValue,req.entity_id)
                inspection=db.get(Inspection,value.inspection_id) if value else None
                store_id=inspection.store_id if inspection else None
            db.add(Photo(entity_type=req.entity_type,entity_id=req.entity_id,store_id=store_id,uploaded_by=user.id,telegram_file_id=p["file_id"],telegram_file_unique_id=p.get("file_unique_id"),telegram_chat_id=chat.get("id"),telegram_message_id=msg.get("message_id"),label=req.label))
            req.status="done"
            send_message(tid,"✅ Фото прикреплено.")
