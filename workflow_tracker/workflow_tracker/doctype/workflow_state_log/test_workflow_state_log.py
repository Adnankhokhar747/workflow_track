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
		self.assertEqual(log[0].changed_by_name, frappe.utils.get_fullname("Administrator"))
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
		# unambiguous in this workflow (only one transition goes Draft -> Pending Approval),
		# so the action name is inferred even though Version history doesn't record it directly
		self.assertEqual(log[0].action, "Submit for Approval")
		self.assertEqual(log[0].changed_by, "Administrator")
		self.assertEqual(log[0].changed_by_name, frappe.utils.get_fullname("Administrator"))
		self.assertIsNotNone(log[0].changed_on)

		# re-running must not duplicate rows that are already there
		second_summary = backfill_workflow_log_history(document_type=TEST_DOCTYPE)
		self.assertEqual(second_summary[TEST_DOCTYPE]["documents_updated"], 0)
		self.assertEqual(len(frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")), 1)

	def test_history_backfill_fills_gap_for_partially_tracked_document(self):
		"""A document that transitioned once before this app existed and again afterwards
		already has some live-tracked rows - only the missing, earlier portion should be
		filled in, without touching or duplicating what's already tracked live."""
		from workflow_tracker.setup import backfill_workflow_log_history

		doc = frappe.get_doc(
			{"doctype": TEST_DOCTYPE, "title": "Partially Tracked Record", "workflow_state": "Draft"}
		).insert(ignore_permissions=True)

		frappe.flags.in_test = False
		try:
			frappe.form_dict["action"] = "Submit for Approval"
			apply_workflow(doc, "Submit for Approval")
			frappe.form_dict.pop("action", None)
		finally:
			frappe.flags.in_test = True

		# simulate the gap: this first transition predates the app, so it has no log row yet
		frappe.db.delete("Workflow State Log", {"parenttype": TEST_DOCTYPE, "parent": doc.name})

		# this second transition happens "live", after the app is already tracking
		doc = frappe.get_doc(TEST_DOCTYPE, doc.name)
		frappe.form_dict["action"] = "Approve"
		apply_workflow(doc, "Approve")
		frappe.form_dict.pop("action", None)

		log_before = frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")
		self.assertEqual(len(log_before), 1, "should only have the live-tracked row so far")
		live_row_name = log_before[0].name

		summary = backfill_workflow_log_history(document_type=TEST_DOCTYPE)
		self.assertEqual(summary[TEST_DOCTYPE]["documents_updated"], 1)

		log_after = frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")
		self.assertEqual(len(log_after), 2)

		self.assertEqual(log_after[0].from_state, "Draft")
		self.assertEqual(log_after[0].to_state, "Pending Approval")
		self.assertEqual(log_after[0].action, "Submit for Approval")

		# the original live-tracked row must be untouched, not replaced
		self.assertEqual(log_after[1].name, live_row_name)
		self.assertEqual(log_after[1].from_state, "Pending Approval")
		self.assertEqual(log_after[1].to_state, "Approved")
		self.assertEqual(log_after[1].action, "Approve")

		second_summary = backfill_workflow_log_history(document_type=TEST_DOCTYPE)
		self.assertEqual(second_summary[TEST_DOCTYPE]["documents_updated"], 0)
		self.assertEqual(len(frappe.get_doc(TEST_DOCTYPE, doc.name).get("workflow_log")), 2)

	def test_unrelated_save_does_not_add_log_row(self):
		doc = frappe.get_doc(
			{"doctype": TEST_DOCTYPE, "title": "Untouched Record", "workflow_state": "Draft"}
		).insert(ignore_permissions=True)

		doc.title = "Renamed, No State Change"
		doc.save(ignore_permissions=True)

		doc = frappe.get_doc(TEST_DOCTYPE, doc.name)
		self.assertEqual(len(doc.get("workflow_log")), 0)

	def test_activating_a_workflow_turns_on_track_changes(self):
		"""Some standard doctypes (e.g. Material Request) don't have Track Changes on by
		default, which silently means their history can never be reconstructed later. Enabling
		a workflow for a doctype should turn it on going forward."""
		doctype = "WF Tracker No Track Changes Doc"
		workflow_name = "WF Tracker No Track Changes Workflow"

		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)

		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": doctype,
				"module": "Workflow Tracker",
				"custom": 1,
				"naming_rule": "Random",
				"autoname": "hash",
				"fields": [{"fieldname": "workflow_state", "fieldtype": "Data", "label": "Workflow State"}],
				"track_changes": 0,
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
				],
			}
		).insert(ignore_permissions=True)

		self.assertFalse(frappe.get_meta(doctype).get("track_changes"))

		try:
			frappe.get_doc(
				{
					"doctype": "Workflow",
					"workflow_name": workflow_name,
					"document_type": doctype,
					"workflow_state_field": "workflow_state",
					"is_active": 1,
					"send_email_alert": 0,
					"states": [{"state": "Draft", "doc_status": "0", "allow_edit": "System Manager"}],
					"transitions": [],
				}
			).insert(ignore_permissions=True)

			self.assertTrue(frappe.get_meta(doctype).get("track_changes"))
		finally:
			frappe.delete_doc("Workflow", workflow_name, force=True, ignore_permissions=True)
			frappe.db.delete("Property Setter", {"doc_type": doctype, "property": "track_changes"})
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
