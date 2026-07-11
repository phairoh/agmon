;;; agmon.el --- Monitor headless Claude Code runs  -*- lexical-binding: t; -*-

;; Copyright (C) 2026  Aaron Trachtman

;; Author: Aaron Trachtman <phairoh@gmail.com>
;; Keywords: tools, processes
;; Version: 0.1.0
;; Package-Requires: ((emacs "30.1") (plz "0.9"))
;; URL: https://github.com/phairoh/agmon

;; This file is not part of GNU Emacs.

;;; Commentary:

;; agmon is a monitoring system for headless Claude Code runs.  A wrapper
;; spools stream-json events to disk, a collector ingests them into SQLite,
;; and a FastAPI server exposes them over the tailnet.  This file is the
;; Emacs client: it hits that HTTP API and renders the run fleet.
;;
;; Entry point: `M-x agmon' opens the *agmon* buffer, a list of every run
;; in the spool.  Set `agmon-url' to point at your server.
;;
;; This is stage 1: a read-only run list.  Faces, live refresh, per-run
;; detail buffers, and event tailing arrive in later stages.

;;; Code:

(require 'plz)
(require 'tabulated-list)
(require 'let-alist)
(require 'subr-x)

;;;; Customization

(defgroup agmon nil
  "Monitor headless Claude Code runs over HTTP."
  :group 'tools
  :prefix "agmon-")

(defcustom agmon-url (or (getenv "AGMON_URL") "http://localhost:8400")
  "Base URL of the agmon server, without a trailing slash.
Defaults to the AGMON_URL environment variable, or
\"http://localhost:8400\" when that is unset."
  :type 'string
  :group 'agmon)

;;;; HTTP transport
;;
;; Every request in this package goes through `agmon--request'.  Nothing
;; else calls plz directly, so the transport can be stubbed in tests and
;; swapped later.
;;
;; JSON representation, chosen once here and used everywhere: JSON objects
;; become alists (keys are symbols), JSON arrays become Lisp lists, and
;; both JSON null and false become nil.  This lets us reach into payloads
;; with `let-alist' / `alist-get' and walk arrays with `mapcar'/`dolist'.

(defun agmon--request (path)
  "Perform a synchronous GET of PATH and return the parsed JSON body.
PATH is a route beginning with a slash, e.g. \"/v1/runs\"; it is
appended to `agmon-url'.  See the commentary above for the JSON
representation.  Signals a `plz-error' if the request fails."
  (let ((url (concat (string-remove-suffix "/" agmon-url) path)))
    (plz 'get url
      :as (lambda ()
            (json-parse-buffer :object-type 'alist
                               :array-type 'list
                               :null-object nil
                               :false-object nil)))))

(defun agmon--runs (&optional status limit)
  "Fetch the run list as a list of alists, newest first.
STATUS filters on the raw meta status; LIMIT caps the number of rows.
Both are optional and passed through to GET /v1/runs."
  (let ((query (string-join
                (delq nil
                      (list (and status (format "status=%s" status))
                            (and limit (format "limit=%d" limit))))
                "&")))
    (alist-get 'runs
               (agmon--request (concat "/v1/runs"
                                       (if (string-empty-p query) ""
                                         (concat "?" query)))))))

;;;; Pure render layer
;;
;; These functions turn parsed JSON into `tabulated-list' structures.
;; They take data and return data -- no network, no buffer mutation --
;; which is what makes them ert-testable.

(defun agmon--short-id (run-id)
  "Return the short, memorable tail of RUN-ID (its hex suffix).
Run ids look like \"20260710T012416-efb89a\"; the tail after the
final dash is the part an operator recognizes."
  (if (string-match "-\\([0-9a-f]+\\)\\'" run-id)
      (match-string 1 run-id)
    run-id))

(defun agmon--abbrev-path (path)
  "Abbreviate PATH to its final two components for compact display.
\"/home/aaron/src/agmon\" becomes \".../src/agmon\"; shorter paths
are returned unchanged."
  (let ((parts (split-string (directory-file-name path) "/" t)))
    (if (> (length parts) 2)
        (concat ".../" (string-join (last parts 2) "/"))
      path)))

(defun agmon--format-started (iso)
  "Render ISO-8601 timestamp ISO compactly as \"MM-DD HH:MM\".
Returns ISO unchanged if it does not match.  Stage 2 replaces this
with human-relative ages."
  (if (and iso (string-match
                "\\`[0-9]\\{4\\}-\\([0-9]\\{2\\}-[0-9]\\{2\\}\\)T\\([0-9]\\{2\\}:[0-9]\\{2\\}\\)"
                iso))
      (concat (match-string 1 iso) " " (match-string 2 iso))
    (or iso "")))

(defun agmon--run-entry (run)
  "Build a `tabulated-list' entry from RUN, an alist for one run.
Returns (ID VECTOR): ID is the full run id, carried on the row so
later stages can recover it with `tabulated-list-get-id'; VECTOR
holds the column cells for `agmon-list-mode'."
  (let-alist run
    (list .run_id
          (vector (agmon--short-id .run_id)
                  (or .effective_status "")
                  (agmon--abbrev-path (or .cwd ""))
                  (agmon--format-started .started_at)))))

(defun agmon--run-entries (runs)
  "Return a `tabulated-list-entries' value.
RUNS is a list of run alists."
  (mapcar #'agmon--run-entry runs))

;;;; Run list mode

(define-derived-mode agmon-list-mode tabulated-list-mode "Agmon"
  "Major mode for browsing the agmon run fleet.

\\{agmon-list-mode-map}"
  ;; Column format: (NAME WIDTH SORTABLE).  A non-nil SORTABLE makes the
  ;; header clickable to sort by that column.
  (setq tabulated-list-format
        [("Id" 8 t)
         ("Status" 12 t)
         ("Cwd" 26 t)
         ("Started" 14 t)])
  (setq tabulated-list-padding 1)
  ;; `g' (revert-buffer, inherited from `special-mode') calls this hook,
  ;; then reprints from `tabulated-list-entries' -- so refresh is free.
  (add-hook 'tabulated-list-revert-hook #'agmon--list-refresh nil t)
  (tabulated-list-init-header))

(defun agmon--list-refresh ()
  "Re-fetch the run list into `tabulated-list-entries'.
Installed on `tabulated-list-revert-hook', so `g' refreshes."
  (setq tabulated-list-entries (agmon--run-entries (agmon--runs))))

;;;###autoload
(defun agmon ()
  "Open the *agmon* buffer listing every run in the fleet."
  (interactive)
  (let ((buf (get-buffer-create "*agmon*")))
    (with-current-buffer buf
      (agmon-list-mode)
      (agmon--list-refresh)
      (tabulated-list-print))
    (pop-to-buffer buf)))

;;;; Development helpers

(defvar agmon--timers nil
  "List of timers created by agmon.
Later stages push refresh/poll timers here so `agmon-dev-reset' and
cleanup hooks can cancel them all.")

(defun agmon-dev-reset ()
  "Cancel every agmon timer and kill every agmon buffer.

DEVELOPMENT TOOL, not for end users.  Re-evaluating this file during
development leaves stale timers firing old callbacks and orphaned
buffers around; this wipes agmon's live state clean and reports what
it did."
  (interactive)
  (let ((ntimers (length agmon--timers))
        (nbuffers 0))
    (dolist (timer agmon--timers)
      (cancel-timer timer))
    (setq agmon--timers nil)
    (dolist (buf (buffer-list))
      (when (string-prefix-p "*agmon" (buffer-name buf))
        (setq nbuffers (1+ nbuffers))
        (kill-buffer buf)))
    (message "agmon-dev-reset: cancelled %d timer(s), killed %d buffer(s)"
             ntimers nbuffers)))

(provide 'agmon)
;;; agmon.el ends here
