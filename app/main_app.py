from fastapi import FastAPI
from app.api import auth,admin,reference,orders,order_settings,work_schedule,employees,shifts,inspections,tasks,tasks_v2,cash,mentoring,plans,ratings,broadcasts,photos,dashboard,ai


def register_routers(app:FastAPI):
    for router in [auth.router,admin.router,reference.router,orders.router,order_settings.router,work_schedule.router,employees.router,shifts.router,inspections.router,tasks.router,tasks_v2.router,cash.router,mentoring.router,plans.router,ratings.router,broadcasts.router,photos.router,dashboard.router,ai.router]:
        app.include_router(router)
