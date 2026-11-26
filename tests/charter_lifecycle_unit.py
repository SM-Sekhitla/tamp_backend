import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.controllers import passenger_dispatch as dispatch

class CharterLifecycleTests(unittest.TestCase):
    def row(self, return_trip=True):
        vehicle = {"id": "bus-1"}
        record = {"status": "CONFIRMED", "customer": "Customer", "supplier": "Transporter", "notes": "",
                  "outbound": {"stops": []}, "quote": {"start": "2099-01-01T10:00:00+00:00", "total": "7500.00", "vat": "EXCL", "vehicles": [vehicle]},
                  "driverJobs": {"bus-1": {"status": "ACCEPTED"}}}
        if return_trip: record["returnLeg"] = {"stops": []}
        return SimpleNamespace(id="charter-1", supplier_id="supplier", organisation_id="customer", data=record), vehicle
    def advance(self, row, vehicle, leg, action, revision):
        with patch.object(dispatch, "notify_organisation"), patch.object(dispatch, "require_permission"), patch.object(dispatch, "driver_job", side_effect=lambda row, vehicle: row.data):
            return dispatch.advance_leg(None, row, vehicle, SimpleNamespace(id="driver"), SimpleNamespace(leg=leg, action=action, revision=revision))
    def test_accepted_driver_can_start_before_future_reservation(self):
        row, v = self.row()
        self.advance(row, v, "outbound", "START", 0)
        self.assertEqual(row.data["driverJobs"][v["id"]]["progress"]["outbound"]["stage"], "TO_PICKUP")
    def test_return_completion_required_for_invoice_and_retry_is_idempotent(self):
        row, v = self.row()
        for leg in ("outbound", "returnLeg"):
            for revision, action in enumerate(("START", "ARRIVE_PICKUP", "BOARD", "COMPLETE")):
                self.advance(row, v, leg, action, revision)
            if leg == "outbound":
                self.assertFalse(dispatch.vehicle_complete(row.data, v["id"]))
                self.assertNotIn("invoice", row.data)
        self.assertTrue(dispatch.vehicle_complete(row.data, v["id"]))
        invoice = dict(row.data["invoice"])
        self.advance(row, v, "returnLeg", "COMPLETE", 3)
        self.assertEqual(invoice, row.data["invoice"])
    def test_one_bus_does_not_complete_another(self):
        row, v = self.row(False)
        row.data["quote"]["vehicles"].append({"id": "bus-2"})
        row.data["driverJobs"]["bus-2"] = {"status": "ACCEPTED"}
        for revision, action in enumerate(("START", "ARRIVE_PICKUP", "BOARD", "COMPLETE")):
            self.advance(row, v, "outbound", action, revision)
        self.assertTrue(dispatch.vehicle_complete(row.data, "bus-1"))
        self.assertFalse(dispatch.vehicle_complete(row.data, "bus-2"))
        self.assertNotIn("invoice", row.data)

if __name__ == "__main__": unittest.main()
