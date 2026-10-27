"""Static content pages."""

from fastapi import APIRouter, Request

from app.templating import render

router = APIRouter()


@router.get("/datenschutz")
def privacy(request: Request):
    return render(request, "datenschutz.html")


@router.get("/offline")
def offline(request: Request):
    return render(request, "offline.html")
