"""Job monitor action definitions and shortcuts with explicit window callbacks."""

from PyQt6.QtGui import QAction

from .store import JOB_EXTENSION
from .ui_actions import make_action

FORCE_ACTION_TEXT = "Force Run Now"
RECHECK_ACTION_TEXT = "Re-check State"


def create_job_actions(parent, callbacks):
    specs = (
        ("new", "New Job...", callbacks["open_submit_dialog"], "Submit a new calculation."),
        ("hosts", "Hosts...", callbacks["open_hosts_dialog"], "Manage host profiles."),
        (
            "refresh",
            "Refresh Now",
            callbacks["_refresh_now"],
            "Ask every host with active jobs for their status now.",
        ),
        (
            "reload",
            "Reload List",
            callbacks["_reload_jobs"],
            "Re-read the job file to pick up changes from another Job Manager window.",
        ),
        (
            "host_monitor",
            "Host Monitor...",
            callbacks["open_host_monitor"],
            "Live load and memory per host, sampled only while that window is open.",
        ),
        (
            "settings",
            "Settings...",
            callbacks["open_settings"],
            "Polling, results, notifications, the task bar and the tray, the local API.",
        ),
        (
            "cancel",
            "Cancel Job",
            callbacks["_cancel_selected"],
            "Cancel the selected job on its host.",
        ),
        ("download", "Download", callbacks["_download_selected"], "Choose results to download."),
        (
            "open",
            "Open Result",
            callbacks["_open_selected_result"],
            "Open one of this job's output files in MoleditPy.",
        ),
        (
            "tail",
            "Tail Log",
            callbacks["_tail_selected"],
            "Read the end of the job's log in a window of its own.",
        ),
        (
            "tail_file",
            "Tail File...",
            callbacks["_tail_specific_file"],
            "Read the tail of a chosen remote output/log file in the job's directory.",
        ),
        (
            "details",
            "Details",
            callbacks["_show_details"],
            "Everything recorded about this job, and the script that ran.",
        ),
        (
            "resubmit",
            "Resubmit",
            callbacks["_resubmit_selected"],
            "Open the submit wizard with this job's host, resources and input files.",
        ),
        (
            "remove",
            "Remove",
            callbacks["_remove_selected"],
            "Remove the selected job from this list.",
        ),
        (
            "open_default",
            "Default List",
            callbacks["_use_default_job_list"],
            "Back to the job list this plugin keeps in ~/.moleditpy/job_manager/.",
        ),
        (
            "open_list",
            "Open List...",
            callbacks["_open_job_list_file"],
            f"Open a saved job list ({JOB_EXTENSION}). A cleared list opens read only.",
        ),
        (
            "save_as",
            "Save As...",
            lambda: callbacks["_export"](JOB_EXTENSION),
            f"Save the job list to a {JOB_EXTENSION} file, openable again from here.",
        ),
        (
            "export_csv",
            "Export CSV...",
            lambda: callbacks["_export"](".csv"),
            "Write one row per job: state, exit code, timings, paths.",
        ),
        (
            "rebuild",
            "Rebuild from Folder...",
            callbacks["_rebuild_from_folder"],
            "Build a read-only job list from results already on disk.",
        ),
        (
            "archive",
            "Load Archive...",
            callbacks["_load_archive"],
            "View a previously cleared job list, read only.",
        ),
        (
            "clear",
            "Clear List...",
            callbacks["_clear_jobs"],
            "Empty the table, saving a dated copy first. Nothing on the host is deleted.",
        ),
        (
            "force",
            FORCE_ACTION_TEXT,
            callbacks["_force_selected"],
            "Start a waiting helper-queue job now.",
        ),
        (
            "recheck",
            RECHECK_ACTION_TEXT,
            callbacks["_recheck_selected"],
            "Look for evidence of a lost job.",
        ),
    )
    actions: dict[str, QAction] = {
        key: make_action(parent, text, callback, tooltip) for key, text, callback, tooltip in specs
    }
    for key, shortcut in (
        ("new", "Ctrl+N"),
        ("open_list", "Ctrl+O"),
        ("save_as", "Ctrl+Shift+S"),
        ("refresh", "F5"),
        ("reload", "Ctrl+R"),
    ):
        actions[key].setShortcut(shortcut)
    job_menu_actions = tuple(
        actions[key] if key is not None else None
        for key in (
            "open",
            "download",
            "tail",
            "tail_file",
            "details",
            None,
            "resubmit",
            "force",
            "recheck",
            None,
            "cancel",
            "remove",
        )
    )
    return actions, job_menu_actions
