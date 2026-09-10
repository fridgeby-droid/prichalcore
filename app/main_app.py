from fastapi import FastAPI
from app.api import auth,admin,reference,orders,order_settings,shifts,inspections,tasks,cash,mentoring,plans,ratings,broadcasts,photos,dashboard,ai


def register_routers(app:FastAPI):
    for router in [auth.router,admin.router,reference.router,orders.router,order_settings.router,shifts.router,inspections.router,tasks.router,cash.router,mentoring.router,plans.router,ratings.router,broadcasts.router,photos.router,dashboard.router,ai.router]:
        app.include_router(router)
