"""
OS Template Service - Scans and manages OS installation templates from disk.
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Literal
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Base path for OS templates
TEMPLATES_DIR = Path(__file__).parent.parent.parent / "os_templates"


class PasswordGenerateConfig(BaseModel):
    """Password generation configuration"""
    enabled: bool = Field(default=True, description="Whether password generation is enabled")
    length: int = Field(default=16, ge=4, le=128, description="Password length")
    charset: Literal["alphanumeric", "alphanumeric_symbols", "numeric", "alphabetic"] = Field(
        default="alphanumeric",
        description="Character set to use"
    )
    exclude_ambiguous: bool = Field(
        default=True,
        description="Exclude ambiguous characters (0, O, 1, I, l, 5, S, 2, Z)"
    )


class TemplateParameter(BaseModel):
    """Template parameter definition"""
    type: str = Field(..., description="Parameter type: text, password, select, number, boolean")
    label: str = Field(..., description="Display label")
    required: bool = Field(default=False, description="Whether parameter is required")
    default: Optional[Any] = Field(default=None, description="Default value")
    options: Optional[List[str]] = Field(default=None, description="Options for select type")
    help: Optional[str] = Field(default=None, description="Help text")
    generate: Optional[PasswordGenerateConfig] = Field(default=None, description="Password generation configuration (for password type)")


class OSTemplate(BaseModel):
    """OS Installation Template"""
    id: str = Field(..., description="Unique template ID")
    name: str = Field(..., description="Display name")
    description: str = Field(..., description="Template description")
    os_type: str = Field(..., description="OS type: windows, linux, other")
    script: Optional[str] = Field(default=None, description="Installation script filename (e.g., install.sh)")
    disk_image: Optional[str] = Field(default=None, description="Disk image path relative to project root (e.g., disk_images/os.iso)")
    parameters: Dict[str, TemplateParameter] = Field(default_factory=dict, description="Template parameters")
    kernel_url: Optional[str] = Field(default=None, description="Kernel URL for Linux templates")
    initrd_url: Optional[str] = Field(default=None, description="Initrd URL for Linux templates")
    user_reinstallable: bool = Field(default=False, description="Whether external users can reinstall this OS via billing API")
    template_dir: Optional[Path] = Field(default=None, exclude=True, description="Template directory path")


class OSTemplateService:
    """Service for managing OS installation templates"""
    
    def __init__(self, templates_dir: Path = TEMPLATES_DIR):
        self.templates_dir = templates_dir
        self._templates: Dict[str, OSTemplate] = {}
        self._scan_templates()
    
    def _scan_templates(self) -> None:
        """Scan templates directory for available templates"""
        self._templates = {}
        
        if not self.templates_dir.exists():
            logger.warning(f"Templates directory does not exist: {self.templates_dir}")
            return
        
        logger.info(f"Scanning templates directory: {self.templates_dir}")
        
        for template_dir in self.templates_dir.iterdir():
            if not template_dir.is_dir():
                continue
            
            template_json = template_dir / "template.json"
            if not template_json.exists():
                logger.warning(f"Template directory {template_dir.name} missing template.json")
                continue
            
            try:
                with open(template_json, 'r') as f:
                    template_data = json.load(f)
                
                # Convert parameters dict to TemplateParameter objects
                parameters = {}
                for param_name, param_data in template_data.get("parameters", {}).items():
                    # Handle generate config if present
                    param_dict = dict(param_data)
                    if "generate" in param_dict and isinstance(param_dict["generate"], dict):
                        param_dict["generate"] = PasswordGenerateConfig(**param_dict["generate"])
                    parameters[param_name] = TemplateParameter(**param_dict)
                
                template = OSTemplate(
                    id=template_data.get("id", template_dir.name),
                    name=template_data.get("name", template_dir.name),
                    description=template_data.get("description", ""),
                    os_type=template_data.get("os_type", "other"),
                    script=template_data.get("script"),
                    disk_image=template_data.get("disk_image"),
                    parameters=parameters,
                    kernel_url=template_data.get("kernel_url"),
                    initrd_url=template_data.get("initrd_url"),
                    user_reinstallable=template_data.get("user_reinstallable", False),
                    template_dir=template_dir
                )
                
                self._templates[template.id] = template
                logger.info(f"Loaded template: {template.id} - {template.name}")
            
            except Exception as e:
                logger.error(f"Failed to load template from {template_dir}: {e}", exc_info=True)
    
    def get_all_templates(self) -> List[OSTemplate]:
        """Get all available templates"""
        return list(self._templates.values())
    
    def get_template(self, template_id: str) -> Optional[OSTemplate]:
        """Get a specific template by ID"""
        return self._templates.get(template_id)
    
    def get_template_script_path(self, template_id: str) -> Optional[Path]:
        """Get the path to the installation script for a template"""
        template = self.get_template(template_id)
        if not template or not template.template_dir:
            return None
        
        # Use script field from template.json if specified
        if template.script:
            script_path = template.template_dir / template.script
            if script_path.exists():
                return script_path
        
        # Fallback: Look for common script names
        script_names = ["install.sh", "install.bash", "setup.sh"]
        for script_name in script_names:
            script_path = template.template_dir / script_name
            if script_path.exists():
                return script_path
        
        return None
    
    def get_template_files(self, template_id: str) -> List[Path]:
        """Get all files in a template directory"""
        template = self.get_template(template_id)
        if not template or not template.template_dir:
            return []
        
        files = []
        for file_path in template.template_dir.iterdir():
            if file_path.is_file():
                files.append(file_path)
        
        return files
    
    def reload_templates(self) -> None:
        """Reload templates from disk"""
        logger.info("Reloading templates...")
        self._scan_templates()


# Global service instance
_template_service: Optional[OSTemplateService] = None


def get_template_service() -> OSTemplateService:
    """Get the global template service instance"""
    global _template_service
    if _template_service is None:
        _template_service = OSTemplateService()
    return _template_service
