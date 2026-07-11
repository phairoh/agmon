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
(require 'iso8601)
(require 'seq)

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

(defcustom agmon-list-columns '(id status cwd age cost task)
  "Columns shown in the run list, left to right.
Each symbol names a column defined in `agmon--column-specs'.  This one
list controls both order and visibility: reorder it to reorder the
columns, and drop a symbol to hide that column (say, `cost').  After
changing it, run \\[agmon] to rebuild the list with the new layout.

Available columns: `id', `status', `cwd', `age', `cost', `task'."
  :type '(repeat symbol)
  :group 'agmon)

;;;; Faces
;;
;; One face per effective status, applied to the Status cell.  They inherit
;; from standard faces (`success', `error', `warning', `shadow') so they
;; track whatever theme the operator runs.  The "act now" states -- running,
;; stalled, error -- are bold so they pop; finished recedes to `shadow'
;; because a fleet is mostly finished runs and you rarely need to look at
;; them.  Retune any of these with `M-x customize-group RET agmon'.

(defface agmon-status-running '((t :inherit success :weight bold))
  "Face for the Status cell of a running run."
  :group 'agmon)

(defface agmon-status-finished '((t :inherit shadow))
  "Face for the Status cell of a finished run."
  :group 'agmon)

(defface agmon-status-error '((t :inherit error :weight bold))
  "Face for the Status cell of a run whose task failed."
  :group 'agmon)

(defface agmon-status-died '((t :inherit error))
  "Face for the Status cell of a run whose wrapper died without finalizing."
  :group 'agmon)

(defface agmon-status-stalled '((t :inherit warning :weight bold))
  "Face for the Status cell of a stalled run (alive but quiet)."
  :group 'agmon)

(defface agmon-status-interrupted '((t :inherit warning))
  "Face for the Status cell of an interrupted run (the retryable kind)."
  :group 'agmon)

(defun agmon--status-face (status)
  "Return the face symbol for effective STATUS, or `default' if unknown."
  (pcase status
    ("running" 'agmon-status-running)
    ("finished" 'agmon-status-finished)
    ("error" 'agmon-status-error)
    ("died" 'agmon-status-died)
    ("stalled" 'agmon-status-stalled)
    ("interrupted" 'agmon-status-interrupted)
    (_ 'default)))

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

(defun agmon--parse-time (iso)
  "Parse ISO-8601 string ISO to an Emacs time value, or nil on failure."
  (when (and iso (stringp iso))
    (ignore-errors (encode-time (iso8601-parse iso)))))

(defun agmon--format-age (secs)
  "Render SECS as a compact human age: \"4m\", \"2h13m\", \"3d\".
SECS is a non-negative integer count of seconds, or nil (rendered as
the empty string)."
  (cond
   ((null secs) "")
   ((< secs 60) (format "%ds" secs))
   ((< secs 3600) (format "%dm" (/ secs 60)))
   ((< secs 86400)
    (let ((h (/ secs 3600))
          (m (/ (% secs 3600) 60)))
      (if (zerop m) (format "%dh" h) (format "%dh%dm" h m))))
   (t (format "%dd" (/ secs 86400)))))

(defun agmon--format-cost (cost)
  "Render COST as \"$%.2f\", or the empty string when COST is not a number."
  (if (numberp cost) (format "$%.2f" cost) ""))

(defun agmon--run-age-seconds (run now)
  "Return RUN's age in seconds as of NOW, or nil if unknown.
Age is time-since-started for every run, so the column reads
consistently and sorts by recency.  NOW is an Emacs time value."
  (let-alist run
    (let ((start (agmon--parse-time .started_at)))
      (when start
        (max 0 (floor (float-time (time-subtract now start))))))))

(defun agmon--cell-sort-value (cell)
  "Return the numeric `agmon-sort' text property carried on CELL, or 0.
CELL is a tabulated-list cell: a (propertized) string, or a
\(STRING . PROPS) cons."
  (let ((s (if (consp cell) (car cell) cell)))
    (or (and (stringp s) (> (length s) 0)
             (get-text-property 0 'agmon-sort s))
        0)))

(defun agmon--numeric-sorter (col)
  "Return a `tabulated-list' sort predicate for column index COL.
It compares rows by the `agmon-sort' property stashed on that column's
cell, so numeric columns (age, cost) sort by value rather than by the
lexicographic order of their rendered text."
  (lambda (a b)
    (< (agmon--cell-sort-value (aref (cadr a) col))
       (agmon--cell-sort-value (aref (cadr b) col)))))

(defconst agmon--column-specs
  '((id     :name "Id"     :width 8  :sort t)
    (status :name "Status" :width 11 :sort t)
    (cwd    :name "Cwd"    :width 20 :sort t)
    (age    :name "Age"    :width 6  :numeric t :right-align t)
    (cost   :name "Cost"   :width 7  :numeric t :right-align t)
    (task   :name "Task"   :width 44))
  "Registry of every run-list column, keyed by symbol.
Each entry is (KEY :name NAME :width W [:sort t] [:numeric t]
[:right-align t]).  `agmon-list-columns' selects which of these to
show and in what order; `agmon--run-cell' renders each KEY's cell.
A `:numeric' column sorts by a stashed number (see
`agmon--numeric-sorter') rather than by its rendered text.")

(defun agmon--run-cell (key run now)
  "Render the cell for column KEY of RUN, as of time NOW.
Returns a possibly-propertized string.  KEY is one of the keys in
`agmon--column-specs'.  Status carries a face; the numeric columns
carry an invisible `agmon-sort' property holding their raw value."
  (let-alist run
    (pcase key
      ('id (agmon--short-id .run_id))
      ('status (let ((s (or .effective_status "")))
                 (propertize s 'face (agmon--status-face s))))
      ('cwd (agmon--abbrev-path (or .cwd "")))
      ('age (let ((start (agmon--parse-time .started_at)))
              (propertize (agmon--format-age (agmon--run-age-seconds run now))
                          'agmon-sort (if start (float-time start) 0))))
      ('cost (propertize (agmon--format-cost .total_cost_usd)
                         'agmon-sort (if (numberp .total_cost_usd)
                                         .total_cost_usd 0)))
      ('task (agmon--truncate (agmon--oneline (or .prompt_preview "")) 100))
      (_ ""))))

(defun agmon--run-entry (run &optional now)
  "Build a `tabulated-list' entry from RUN, an alist for one run.
Returns (ID VECTOR): ID is the full run id, carried on the row so
later stages can recover it with `tabulated-list-get-id'; VECTOR holds
one cell per column named in `agmon-list-columns', in that order.  NOW
is an Emacs time value for the age column, defaulting to the current
time."
  (setq now (or now (current-time)))
  (list (alist-get 'run_id run)
        (vconcat (mapcar (lambda (key) (agmon--run-cell key run now))
                         agmon-list-columns))))

(defun agmon--run-entries (runs &optional now)
  "Return a `tabulated-list-entries' value.
RUNS is a list of run alists; NOW is passed to `agmon--run-entry'."
  (setq now (or now (current-time)))
  (mapcar (lambda (run) (agmon--run-entry run now)) runs))

(defun agmon--list-format ()
  "Build a `tabulated-list-format' vector from `agmon-list-columns'.
A column's index is its position in that list, so the numeric sorters
\(which read a cell by index) stay aligned with the entry vectors
`agmon--run-entry' builds in the same order."
  (vconcat
   (seq-map-indexed
    (lambda (key idx)
      (let* ((spec (alist-get key agmon--column-specs))
             (name (plist-get spec :name))
             (width (plist-get spec :width))
             (sort (cond ((plist-get spec :numeric) (agmon--numeric-sorter idx))
                         ((plist-get spec :sort) t)))
             (base (list name width sort)))
        (if (plist-get spec :right-align)
            (append base '(:right-align t))
          base)))
    agmon-list-columns)))

(defun agmon--default-sort-key ()
  "Return the default sort key: newest-first by Age when Age is shown.
Nil (server order) when the Age column is hidden, since a sort key
naming an absent column would error at print time."
  (when (memq 'age agmon-list-columns)
    (cons "Age" t)))

(defun agmon--oneline (s)
  "Collapse each whitespace sequence in S to a single space, trimmed."
  (string-join (split-string (or s "") "[ \t\n\r]+" t) " "))

(defun agmon--truncate (s width)
  "Truncate S to WIDTH characters, marking the cut with an ellipsis."
  (if (and width (> (length s) width))
      (concat (substring s 0 (max 0 (1- width))) "…")
    s))

;;;; Run list mode

(define-derived-mode agmon-list-mode tabulated-list-mode "Agmon"
  "Major mode for browsing the agmon run fleet.

\\{agmon-list-mode-map}"
  ;; Both the column format and each row's cells are built from the single
  ;; list `agmon-list-columns' (see `agmon--list-format' / `agmon--run-entry'),
  ;; so reordering or hiding a column is a one-line edit to that defcustom.
  (setq tabulated-list-format (agmon--list-format))
  ;; Age's sort value is the start time, so newest-first is the natural
  ;; default; the `t' flips the ascending predicate to descending.
  (setq tabulated-list-sort-key (agmon--default-sort-key))
  (setq tabulated-list-padding 1)
  ;; `g' (revert-buffer, inherited from `special-mode') calls this hook,
  ;; then reprints from `tabulated-list-entries' -- so refresh is free.
  (add-hook 'tabulated-list-revert-hook #'agmon--list-refresh nil t)
  (tabulated-list-init-header))

(defun agmon-sort-default ()
  "Restore the default newest-first sort in an agmon list buffer."
  (interactive)
  (setq tabulated-list-sort-key (agmon--default-sort-key))
  (tabulated-list-init-header)
  (tabulated-list-print t))

;; `o' restores the default sort after a column-header click sends you
;; elsewhere; plain tabulated-list-mode has no key to undo a sort.
(keymap-set agmon-list-mode-map "o" #'agmon-sort-default)

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
