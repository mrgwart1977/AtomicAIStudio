"""Shared request dependencies: DB session, project lookup with scoping."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.models import Project


def get_db(request: Request):
    sf = request.app.state.session_factory
    with sf() as session:
        yield session


def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    """Every project-scoped route resolves through here — no unscoped access."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project
