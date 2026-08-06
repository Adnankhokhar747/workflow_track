import frappe
from frappe.model.workflow import apply_workflow
from frappe.tests.utils import FrappeTestCase

TEST_DOCTYPE = "WF Tracker Test Doc"
TEST_WORKFLOW = "WF Tracker Test Workflow"


class TestWorkflowStateLog(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._create_test_doctype()
		cls._create_test_workflow()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(TEST_DOCTYPE)
		frappe.db.delete("Version", {"ref_doctype": TEST_DOCTYPE})
		frappe.delete_doc("Workflow", TEST_WORKFLOW, force=True, ignore_permissions=True)
		frappe.delete_doc("DocType", TEST_DOCTYPE, force=True, ignore_permissions=True)
		super().tearDownClass()

	@classmethod
	def _create_test_doctype(cls):
		if frappe.db.exists("DocType", TEST_DOCTYPE):
			frappe.delete_doc("DocType", TEST_DOCTYPE, force=True, ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": TEST_DOCTYPE,
				"module": "Workflow Tracker",
				"custom": 1,
				"naming_rule": "Random",
				"autoname": "hash",
				"fields": [
					{"fieldname": "title", "fieldtype": "Data", "label": "Title"},
					{"fieldname": "workflow_state", "fieldtype": "Data", "label": "Workflow State"},
				],
				"track_changes": 1,
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
				],
			}
		).insert(ignore_permissions=True)

	@classmethod
	def _create_test_workflow(cls):
		for state in ("Draft", "Pending Approval", "Approved"):
			if not frappe.db.exists("Workflow State", state):
				frappe.get_doc({"doctype": "Workflow State", "workflow_state_name": state}).insert(
					ignore_permissions=True
				)

		for action in ("Submit for Approval", "Approve"):
			if not frappe.db.exists("Workflow Action Master", action):
				frappe.get_doc(
					{"doctype": "Workflow Action Master", "workflow_action_name": action}
				).insert(ignore_permissions=True)

		if frappe.db.exists("Workflow", TEST_WORKFLOW):
			frappe.delete_doc("Workflow", TEST_WORKFLOW, force=True, ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "Workflow",
				"workflow_name": TEST_WORKFLOW,
				"document_type": TEST_DOCTYPE,
				"workflow_state_field": "workflow_state",
				"is_active": 1,
				"send_email_alert": 0,
				"states": [
					{"state": "Draft", "doc_status": "0", "allow_edit": "System Manager"},
					{"state": "Pending Approval", "doc_status": "0", "allow_edit": "System Manager"},
					{"state": "Approved", "doc_status": "0", "allow_edit": "System Manager"},
				],
				"transitions": [
					{
						"state": "Draft",
						"action": "Submit for Approval",
						"next_state": "Pending Approval",
						"allowed": "System Manager",
					},
					{
						"state": "Pending Approval",
						"action": "Approve",
						"next_state": "Approved",
						"allowed": "System Manager",
					},
				],
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(doctype=TEST_DOCTYPE)

	def test_workflow_log_field_is_attached(self):
		meta = frappe.get_meta(TEST_DOCTYPE)
		self.assertTrue(meta.get_field("workflow_log"), "workflow_log child table was not auto-attached")

	def test_state_transitions_are_logged(self):
		doc = frappe.get_doc(
			{"doctype": TEST_DOCTYPE, "title": "Test Record", "workflow_state": "Draft"}
		).insert(ignore_permissions=True)

		frappe.form_dict["action"] = "Submit for Approval"
		apply_workflow(doc, "Submit for Approval")
		frappe.form_dict.pop("action", None)

		doc = frappe.get_doc(TEST_DOCTYPE, doc.name)
		frappe.form_dict["action"] = "Approve"
		apply_workflow(doc, "Approve")
		frappe.form_dict.pop("action", None)

		doc = frappe.get_doc(TEST_DOCTYPE, doc.name)
		self.assertEqual(doc.workflow_state, "Approved")

		log = doc.get("workflow_log")
		self.assertEqual(len(log), 2)

		self.assertEqual(log[0].from_state, "Draft")
		self.assertEqual(log[0].to_state, "Pending Approval")
		self.assertEqual(log[0].action, "Submit for Approval")
		self.assertEqual(log[0].changed_by, "Administrator")
		self.assertIsNotNone(log[0].changed_on)

		self.assertEqual(log[1].from_state, "Pending Approval")
		self.assertEqual(log[1].to_state, "Approved")
		self.assertEqual(log[1].action, "Approve")

	def test_history_backfill_reconstructs_pre_existing_transitions(self):
		from workflow_tracker.setup import backfill_workflow_log_history

		doc = frappe.get_doc(
			{"doctype": TEST_DOCTYPE, "title": "Pre-existing Record", "workflow_state": "Draft"}
		).insert(ignore_permissions=True)

		# `bench run-tests` skips Version tracking by default (save()'s ignore_version defaults
		# to frappe.flags.in_test) - flip it off just for this transition so a real Version row
		# gets written, mirroring what happens on a live site outside the test runner.
		frappe.flags.in_test = False
		try:
			frappe.form_dict["action"] = "Submit for Approval"
			apply_workflow(doc, "Submit for Approval")
			frappe.form_dict.pop("action", None)
		finally:
			frappe.flags.in_test = True

		# simulate a document that transitioned states before this app tracked anything:
		# the Version history is real, but no Workflow Log rows exist for it yet.
		frappe.db.delete("Workflow State Log", {"parenttype": TEST_DOCTYPE, "parent": doc.name})

		summary = backfill_workflow_log_history(document_type=TEST_DOCTYPE)
		self.assertEqual(summary[TEST_DOCTYPE]["documents_updated"], 1)

		log = frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")
		self.assertEqual(len(log), 1)
		self.assertEqual(log[0].from_state, "Draft")
		self.assertEqual(log[0].to_state, "Pending Approval")
		self.assertEqual(log[0].changed_by, "Administrator")
		self.assertIsNotNone(log[0].changed_on)

		# re-running must not duplicate rows that are already there
		second_summary = backfill_workflow_log_history(document_type=TEST_DOCTYPE)
		self.assertEqual(second_summary[TEST_DOCTYPE]["documents_updated"], 0)
		self.assertEqual(len(frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")), 1)

	def test_unrelated_save_does_not_add_log_row(self):
		doc = frappe.get_doc(
			{"doctype": TEST_DOCTYPE, "title": "Untouched Record", "workflow_state": "Draft"}
		).insert(ignore_permissions=True)

		doc.title = "Renamed, No State Change"
		doc.save(ignore_permissions=True)

		doc = frappe.get_doc(TEST_DOCTYPE, doc.name)
		self.assertEqual(len(doc.get("workflow_log")), 0)
