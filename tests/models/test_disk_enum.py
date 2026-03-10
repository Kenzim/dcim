"""
Tests for DiskType enum and case-insensitive handling
"""
import pytest
from app.models.disk import DiskType, CaseInsensitiveEnum, Disk
from app.models.server import Server
from app.models.location import Location


@pytest.fixture
def test_location(db_session):
    """Create a test location"""
    location = Location(name="Test Location", description="Test")
    db_session.add(location)
    db_session.commit()
    db_session.refresh(location)
    return location


@pytest.fixture
def test_server(db_session, test_location):
    """Create a test server (uses plugin_name string)"""
    server = Server(
        name="test-server",
        server_ip="192.168.1.100",
        location_id=test_location.id,
        plugin_name="ipmi",
        plugin_config={"hostname": "192.168.1.100"}
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server


def test_disk_type_enum_values():
    """Test that DiskType enum has correct uppercase values"""
    assert DiskType.SSD.value == "SSD"
    assert DiskType.HDD.value == "HDD"


def test_disk_type_case_insensitive_missing():
    """Test that _missing_ method handles case-insensitive values"""
    # Test lowercase
    assert DiskType("ssd") == DiskType.SSD
    assert DiskType("hdd") == DiskType.HDD
    
    # Test uppercase
    assert DiskType("SSD") == DiskType.SSD
    assert DiskType("HDD") == DiskType.HDD
    
    # Test mixed case
    assert DiskType("SsD") == DiskType.SSD
    assert DiskType("HdD") == DiskType.HDD


def test_case_insensitive_enum_decorator():
    """Test CaseInsensitiveEnum TypeDecorator"""
    ci_enum = CaseInsensitiveEnum(DiskType)
    
    # Test process_result_value with lowercase
    result = ci_enum.process_result_value("ssd", None)
    assert result == DiskType.SSD
    
    # Test process_result_value with uppercase
    result = ci_enum.process_result_value("SSD", None)
    assert result == DiskType.SSD
    
    # Test process_bind_param
    result = ci_enum.process_bind_param(DiskType.SSD, None)
    assert result == "SSD"
    
    # Test process_bind_param with string
    result = ci_enum.process_bind_param("ssd", None)
    assert result == "SSD"


def test_disk_creation_with_uppercase_enum(db_session, test_server):
    """Test creating a disk with uppercase enum value"""
    disk = Disk(
        server_id=test_server.id,
        type=DiskType.SSD,
        capacity_gb=500
    )
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    
    assert disk.type == DiskType.SSD
    assert disk.type.value == "SSD"


def test_disk_creation_with_lowercase_string(db_session, test_server):
    """Test that disk creation handles lowercase strings via CaseInsensitiveEnum"""
    # This tests the TypeDecorator's process_result_value
    disk = Disk(
        server_id=test_server.id,
        type=DiskType("ssd"),  # This uses _missing_ method
        capacity_gb=500
    )
    db_session.add(disk)
    db_session.commit()
    db_session.refresh(disk)
    
    assert disk.type == DiskType.SSD
    assert disk.type.value == "SSD"


def test_disk_repr(db_session, test_server):
    """Test Disk __repr__ method"""
    disk = Disk(
        server_id=test_server.id,
        type=DiskType.SSD,
        capacity_gb=500
    )
    db_session.add(disk)
    db_session.commit()
    
    repr_str = repr(disk)
    assert "SSD" in repr_str
    assert "500GB" in repr_str
