from typing import Optional

from sqlalchemy.orm import Session

from app.models.vmid_reservation import VMIDReservation


class VMIDReservationDAO:
    @staticmethod
    def get_by_service_id(db: Session, service_id: int) -> Optional[VMIDReservation]:
        return (
            db.query(VMIDReservation)
            .filter(VMIDReservation.service_id == service_id)
            .order_by(VMIDReservation.id.desc())
            .first()
        )

    @staticmethod
    def get_by_cluster_vmid(db: Session, cluster_id: int, vmid: int) -> Optional[VMIDReservation]:
        return (
            db.query(VMIDReservation)
            .filter(VMIDReservation.cluster_id == cluster_id, VMIDReservation.vmid == vmid)
            .first()
        )

    @staticmethod
    def create(db: Session, cluster_id: int, service_id: int, vmid: int) -> VMIDReservation:
        row = VMIDReservation(cluster_id=cluster_id, service_id=service_id, vmid=vmid)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
