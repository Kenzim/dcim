"""
API endpoints for OS installation templates.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List
from app.core.auth import require_admin
from app.services.os_template_service import get_template_service, OSTemplate
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class TemplateResponse(BaseModel):
    """Template response model"""
    id: str
    name: str
    description: str
    os_type: str
    parameters: dict
    kernel_url: str | None = None
    initrd_url: str | None = None
    user_reinstallable: bool = False

    class Config:
        from_attributes = True


@router.get("/", response_model=List[TemplateResponse])
async def list_templates(
    auth: dict = Depends(require_admin)
):
    """List all available OS installation templates"""
    service = get_template_service()
    templates = service.get_all_templates()
    
    return [
        TemplateResponse(
            id=template.id,
            name=template.name,
            description=template.description,
            os_type=template.os_type,
            parameters={
                name: {
                    "type": param.type,
                    "label": param.label,
                    "required": param.required,
                    "default": param.default,
                    "options": param.options,
                    "help": param.help
                }
                for name, param in template.parameters.items()
            },
            kernel_url=template.kernel_url,
            initrd_url=template.initrd_url,
            user_reinstallable=template.user_reinstallable
        )
        for template in templates
    ]


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_template(
    template_id: str,
    auth: dict = Depends(require_admin)
):
    """Get a specific template by ID"""
    service = get_template_service()
    template = service.get_template(template_id)
    
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found"
        )
    
    return TemplateResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        os_type=template.os_type,
        parameters={
            name: {
                "type": param.type,
                "label": param.label,
                "required": param.required,
                "default": param.default,
                "options": param.options,
                "help": param.help
            }
            for name, param in template.parameters.items()
        },
        kernel_url=template.kernel_url,
        initrd_url=template.initrd_url
    )


@router.post("/reload")
async def reload_templates(
    auth: dict = Depends(require_admin)
):
    """Reload templates from disk"""
    service = get_template_service()
    service.reload_templates()
    
    return {"message": "Templates reloaded successfully"}
