from __future__ import annotations
from datetime import datetime
from sqlalchemy import select
from app.db.database import session_scope
from app.db.models import User,PhotoRequest,Photo,ShiftReport,ShiftReportValue,Inspection,InspectionValue,Task,CashCollection,PriceListUploadRequest,PriceList,TaskAttachmentRequest,TaskAttachment,TaskAssignee,KnowledgeMediaUploadRequest,KnowledgeMedia,TrainingQuestionMediaRequest,TrainingQuestion
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
    photos=msg.get("photo") or []
    if photos:
        with session_scope() as db:
            user=db.scalar(select(User).where(User.telegram_id==tid))
            if not user:
                send_message(tid,"Сначала откройте MiniApp и авторизуйтесь.");return
            test_req=db.scalar(select(TrainingQuestionMediaRequest).where(TrainingQuestionMediaRequest.user_id==user.id,TrainingQuestionMediaRequest.status=="waiting",TrainingQuestionMediaRequest.expires_at>datetime.utcnow()).order_by(TrainingQuestionMediaRequest.created_at.desc()))
            if test_req:
                q=db.get(TrainingQuestion,test_req.question_id)
                if not q:
                    test_req.status="expired"
                    send_message(tid,"Вопрос теста не найден.");return
                p=photos[-1]
                q.image_telegram_file_id=p["file_id"]
                q.image_telegram_file_unique_id=p.get("file_unique_id")
                q.image_mime_type="image/jpeg"
                test_req.status="done"
                send_message(tid,"✅ Фото добавлено к вопросу теста. Вернитесь в редактор.")
                return
            kb_req=db.scalar(select(KnowledgeMediaUploadRequest).where(KnowledgeMediaUploadRequest.user_id==user.id,KnowledgeMediaUploadRequest.kind=="photo",KnowledgeMediaUploadRequest.status=="waiting",KnowledgeMediaUploadRequest.expires_at>datetime.utcnow()).order_by(KnowledgeMediaUploadRequest.created_at.desc()))
            if kb_req:
                p=photos[-1]
                db.add(KnowledgeMedia(article_id=kb_req.article_id,uploaded_by=user.id,kind="photo",telegram_file_id=p["file_id"],telegram_file_unique_id=p.get("file_unique_id"),telegram_chat_id=chat.get("id"),telegram_message_id=msg.get("message_id"),file_name=None,mime_type="image/jpeg",file_size=p.get("file_size"),caption=msg.get("caption")))
                kb_req.status="done"
                send_message(tid,"✅ Фото добавлено в базу знаний. Вернитесь в статью, чтобы вставить его в текст.")
                return
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
