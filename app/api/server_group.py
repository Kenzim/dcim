from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServerGroupDAO, ServerDAO
from app.models.server_group import ServerGroup, server_group_association
from app.models.server import Server

router = APIRouter()


class PermittedOptions(BaseModel):
    enable_isos: bool = False
    permitted_isos: List[str] = []
    enable_temp_os: bool = False
    permitted_temp_os: List[str] = []
    enable_scripts: bool = False
    permitted_scripts: List[int] = []
    enable_os_templates: bool = False
    permitted_os_templates: List[str] = []


class ServerGroupCreate(BaseModel):
    name: str
    description: Optional[str] = None
    enable_isos: bool = False
    permitted_isos: List[str] = []
    enable_temp_os: bool = False
    permitted_temp_os: List[str] = []
    enable_scripts: bool = False
    permitted_scripts: List[int] = []
    enable_os_templates: bool = False
    permitted_os_templates: List[str] = []


class ServerGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    enable_isos: Optional[bool] = None
    permitted_isos: Optional[List[str]] = None
    enable_temp_os: Optional[bool] = None
    permitted_temp_os: Optional[List[str]] = None
    enable_scripts: Optional[bool] = None
    permitted_scripts: Optional[List[int]] = None
    enable_os_templates: Optional[bool] = None
    permitted_os_templates: Optional[List[str]] = None


class ServerGroupResponse(BaseModel):
    id: int
    name: str
    description: str | None
    server_count: int = 0
    created_at: str
    updated_at: str
    enable_isos: bool = False
    permitted_isos: List[str] = []
    enable_temp_os: bool = False
    permitted_temp_os: List[str] = []
    enable_scripts: bool = False
    permitted_scripts: List[int] = []
    enable_os_templates: bool = False
    permitted_os_templates: List[str] = []

    class Config:
        from_attributes = True


class ServerGroupDetailResponse(ServerGroupResponse):
    servers: List[dict] = []


class ServerGroupAddServers(BaseModel):
    server_ids: List[int]


@router.get("/", response_model=List[ServerGroupResponse])
async def list_server_groups(
    skip: int = 0,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all server groups"""
    server_groups = ServerGroupDAO.get_all(db, skip=skip, limit=limit)
    
    result = []
    for group in server_groups:
        server_count = len(group.servers) if group.servers else 0
        result.append({
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "server_count": server_count,
            "created_at": group.created_at.isoformat() if group.created_at else None,
            "updated_at": group.updated_at.isoformat() if group.updated_at else None,
            "enable_isos": getattr(group, "enable_isos", False) or False,
            "permitted_isos": list(group.permitted_isos or []),
            "enable_temp_os": getattr(group, "enable_temp_os", False) or False,
            "permitted_temp_os": list(group.permitted_temp_os or []),
            "enable_scripts": getattr(group, "enable_scripts", False) or False,
            "permitted_scripts": list(group.permitted_scripts or []),
            "enable_os_templates": getattr(group, "enable_os_templates", False) or False,
            "permitted_os_templates": list(group.permitted_os_templates or []),
        })
    return result


@router.get("/{group_id}", response_model=ServerGroupDetailResponse)
async def get_server_group(
    group_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a server group by ID with its servers"""
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found"
        )
    
    # Get server details
    servers = []
    if group.servers:
        for server in group.servers:
            servers.append({
                "id": server.id,
                "name": server.name,
                "server_ip": server.server_ip,
                "description": server.description,
            })
    
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "server_count": len(servers),
        "servers": servers,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
        "enable_isos": getattr(group, "enable_isos", False) or False,
        "permitted_isos": list(group.permitted_isos or []),
        "enable_temp_os": getattr(group, "enable_temp_os", False) or False,
        "permitted_temp_os": list(group.permitted_temp_os or []),
        "enable_scripts": getattr(group, "enable_scripts", False) or False,
        "permitted_scripts": list(group.permitted_scripts or []),
        "enable_os_templates": getattr(group, "enable_os_templates", False) or False,
        "permitted_os_templates": list(group.permitted_os_templates or []),
    }


@router.post("/", response_model=ServerGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_server_group(
    group_data: ServerGroupCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new server group"""
    # Check if group with same name already exists
    existing = ServerGroupDAO.get_by_name(db, group_data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server group with this name already exists"
        )
    
    group = ServerGroupDAO.create(
        db,
        name=group_data.name,
        description=group_data.description,
        enable_isos=group_data.enable_isos,
        permitted_isos=group_data.permitted_isos,
        enable_temp_os=group_data.enable_temp_os,
        permitted_temp_os=group_data.permitted_temp_os,
        enable_scripts=group_data.enable_scripts,
        permitted_scripts=group_data.permitted_scripts,
        enable_os_templates=group_data.enable_os_templates,
        permitted_os_templates=group_data.permitted_os_templates,
    )
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "server_count": 0,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
        "enable_isos": getattr(group, "enable_isos", False) or False,
        "permitted_isos": list(group.permitted_isos or []),
        "enable_temp_os": getattr(group, "enable_temp_os", False) or False,
        "permitted_temp_os": list(group.permitted_temp_os or []),
        "enable_scripts": getattr(group, "enable_scripts", False) or False,
        "permitted_scripts": list(group.permitted_scripts or []),
        "enable_os_templates": getattr(group, "enable_os_templates", False) or False,
        "permitted_os_templates": list(group.permitted_os_templates or []),
    }


@router.put("/{group_id}", response_model=ServerGroupResponse)
async def update_server_group(
    group_id: int,
    group_data: ServerGroupUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a server group"""
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found"
        )
    
    if group_data.name is not None:
        if group_data.name != group.name:
            existing = ServerGroupDAO.get_by_name(db, group_data.name)
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Server group with this name already exists"
                )
        group.name = group_data.name
    if group_data.description is not None:
        group.description = group_data.description
    if group_data.enable_isos is not None:
        group.enable_isos = group_data.enable_isos
    if group_data.permitted_isos is not None:
        group.permitted_isos = group_data.permitted_isos
    if group_data.enable_temp_os is not None:
        group.enable_temp_os = group_data.enable_temp_os
    if group_data.permitted_temp_os is not None:
        group.permitted_temp_os = group_data.permitted_temp_os
    if group_data.enable_scripts is not None:
        group.enable_scripts = group_data.enable_scripts
    if group_data.permitted_scripts is not None:
        group.permitted_scripts = group_data.permitted_scripts
    if group_data.enable_os_templates is not None:
        group.enable_os_templates = group_data.enable_os_templates
    if group_data.permitted_os_templates is not None:
        group.permitted_os_templates = group_data.permitted_os_templates

    ServerGroupDAO.update(db, group)
    server_count = len(group.servers) if group.servers else 0
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "server_count": server_count,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
        "enable_isos": getattr(group, "enable_isos", False) or False,
        "permitted_isos": list(group.permitted_isos or []),
        "enable_temp_os": getattr(group, "enable_temp_os", False) or False,
        "permitted_temp_os": list(group.permitted_temp_os or []),
        "enable_scripts": getattr(group, "enable_scripts", False) or False,
        "permitted_scripts": list(group.permitted_scripts or []),
        "enable_os_templates": getattr(group, "enable_os_templates", False) or False,
        "permitted_os_templates": list(group.permitted_os_templates or []),
    }


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server_group(
    group_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a server group"""
    success = ServerGroupDAO.delete(db, group_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found"
        )
    return None


@router.post("/{group_id}/servers", status_code=status.HTTP_200_OK)
async def add_servers_to_group(
    group_id: int,
    request: ServerGroupAddServers,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Add servers to a server group"""
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found"
        )
    
    # Get existing server IDs in the group
    existing_server_ids = {server.id for server in group.servers} if group.servers else set()
    
    # Add new servers
    added_count = 0
    for server_id in request.server_ids:
        if server_id not in existing_server_ids:
            server = ServerDAO.get_by_id(db, server_id)
            if not server:
                continue  # Skip invalid server IDs
            group.servers.append(server)
            added_count += 1
    
    if added_count > 0:
        db.commit()
        db.refresh(group)
    
    return {
        "message": f"Added {added_count} server(s) to group",
        "added_count": added_count,
        "total_servers": len(group.servers) if group.servers else 0
    }


@router.delete("/{group_id}/servers/{server_id}", status_code=status.HTTP_200_OK)
async def remove_server_from_group(
    group_id: int,
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Remove a server from a server group"""
    group = ServerGroupDAO.get_by_id(db, group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server group not found"
        )
    
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    if server in group.servers:
        group.servers.remove(server)
        db.commit()
        db.refresh(group)
        return {"message": "Server removed from group"}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server is not in this group"
        )
