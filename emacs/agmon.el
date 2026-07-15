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
(require 'json)
(require 'js)
(require 'treesit)
(require 'url-util)

;;;; Internal state

(defvar agmon--timers nil
  "All live timers agmon has created, across buffers.
Auto-refresh/poll timers are pushed here (and removed on cleanup) so
`agmon-dev-reset' and `kill-buffer-hook's can cancel them all.")

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

(defcustom agmon-list-columns
  '(id status
    age started runtime
    cwd model
    pipeline phase
    cost labels)
  "Columns shown in the run list, left to right.
Each symbol names a column defined in `agmon--column-specs'.  This one
list controls both order and visibility: reorder it to reorder the
columns, and drop a symbol to hide that column (say, `cost').  After
changing it, run \\[agmon] to rebuild the list with the new layout.

Available columns are the keys of `agmon--column-specs': `id',
`status', `model', `cwd', `age', `started', `runtime', `cost',
`pipeline', `phase', `parent', `labels', `task'."
  :type '(repeat symbol)
  :group 'agmon)

(defcustom agmon-list-refresh-seconds 10
  "Seconds between automatic refreshes of the run list.
The list re-fetches on this interval, but only while a window actually
displays it -- a buried list makes no background requests.  Set to 0 to
disable auto-refresh entirely (\\[revert-buffer] still refreshes by hand)."
  :type 'natnum
  :group 'agmon)

(defcustom agmon-detail-table-cell-width 36
  "Maximum rendered width of a Markdown table cell in the detail buffer.
Cells longer than this are truncated with an ellipsis, so a wide table
stays aligned and narrow enough to read without soft-wrapping.  Raise it
for wide frames, lower it for narrow ones."
  :type 'integer
  :group 'agmon)

(defcustom agmon-tail-poll-seconds 2
  "Seconds between polls of the event tail buffer.
Like the list, the tail only polls while a window shows it.  The tail
stops entirely once the run reaches a terminal status."
  :type 'natnum
  :group 'agmon)

(defcustom agmon-tail-hidden-types '("system" "rate_limit_event")
  "Event types the tail hides by default as low-value noise.
Toggle them on per-buffer with \\<agmon-tail-mode-map>\\[agmon-tail-toggle-all]."
  :type '(repeat string)
  :group 'agmon)

(defcustom agmon-tail-line-width 200
  "Maximum width of a rendered tail line before it is truncated.
Keeps a single event (e.g. a long tool_result) from wrapping across many
screen lines.  Set to 0 to never truncate."
  :type 'natnum
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

;; Detail-buffer faces.  Labels recede; headings and inline code carry
;; colour by inheriting from theme font-lock faces, so they track the
;; user's theme rather than hard-coding hues.

(defface agmon-detail-label '((t :inherit shadow))
  "Face for the aligned field labels in a run detail buffer."
  :group 'agmon)

(defface agmon-detail-heading '((t :inherit (bold font-lock-keyword-face)))
  "Face for section headings (Result, Issues) and rendered Markdown headings."
  :group 'agmon)

(defface agmon-detail-code '((t :inherit font-lock-constant-face))
  "Face for Markdown inline code spans in rendered result text."
  :group 'agmon)

(defface agmon-tail-progress '((t :inherit (bold font-lock-keyword-face)))
  "Face for PROGRESS lines in the event tail -- the bright, watch-for-me line."
  :group 'agmon)

(defface agmon-costs-total '((t :inherit bold))
  "Face for the pinned totals row of the cost rollup."
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

(defun agmon--request (path &optional raw then else)
  "Perform a GET of PATH and return the JSON body.
PATH is a route beginning with a slash, e.g. \"/v1/runs\"; it is
appended to `agmon-url'.  By default the body is parsed per the JSON
representation above; with RAW non-nil it is returned verbatim as a
string (for the raw-JSON escape hatch).

Without THEN the request is synchronous: it returns the body and signals
a `plz-error' on failure.  With THEN it is asynchronous: THEN is called
with the body when it arrives, and the return value is the underlying
process.  ELSE, if given, is called with the `plz-error' on failure;
otherwise failures are reported by `agmon--report-request-failure' (never
by disturbing a buffer)."
  (let ((url (concat (string-remove-suffix "/" agmon-url) path))
        (as (if raw
                'string
              (lambda ()
                (json-parse-buffer :object-type 'alist
                                   :array-type 'list
                                   :null-object nil
                                   :false-object nil)))))
    (if then
        (plz 'get url :as as :then then
          :else (or else (lambda (err) (agmon--report-request-failure path err))))
      (plz 'get url :as as))))

(defun agmon--report-request-failure (path err)
  "Report a failed async request to PATH via the echo area.
ERR is a `plz-error'.  A background refresh must never blank or
half-draw a buffer, so we only message and leave the last good contents
in place."
  (message "agmon: request to %s failed: %S" path err))

(defun agmon--runs (&optional status limit then labels)
  "Fetch the run list as a list of alists, newest first.
STATUS filters on the raw meta status; LIMIT caps the number of rows;
LABELS is a list of \"key=value\" strings, each sent as a `label=' filter
\(the server ANDs them).  Without THEN this is synchronous and returns
the list; with THEN it is asynchronous and THEN is called with the list.
All are optional and passed through to GET /v1/runs."
  (let* ((query (string-join
                 (delq nil
                       (append
                        (list (and status (format "status=%s" status))
                              (and limit (format "limit=%d" limit)))
                        (mapcar (lambda (l) (concat "label=" (url-hexify-string l)))
                                labels)))
                 "&"))
         (path (concat "/v1/runs"
                       (if (string-empty-p query) "" (concat "?" query)))))
    (if then
        (agmon--request path nil
                        (lambda (data) (funcall then (alist-get 'runs data))))
      (alist-get 'runs (agmon--request path)))))

(defun agmon--summary (run-id &optional then)
  "Fetch the parsed /summary payload for RUN-ID.
The reply is a nested alist: `run' (the full record), `status',
`activity', `issues', `metrics', and `result_text'.  Without THEN this
is synchronous and returns the payload; with THEN it is asynchronous and
THEN is called with it."
  (agmon--request (format "/v1/runs/%s/summary" run-id) nil then))

(defun agmon--events (run-id after then &optional else)
  "Async GET a page of RUN-ID's events after seq AFTER; call THEN with the batch.
The batch is an alist with `events' (a list) and `next_after' (the
cursor to poll with next).  ELSE handles a request failure."
  (agmon--request (format "/v1/runs/%s/events?after=%d&limit=500" run-id after)
                  nil then else))

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
  "Abbreviate PATH to its final component for compact display.
\"/home/aaron/src/agmon\" becomes \"agmon\"; a fleet whose runs all
live under one parent shows only the part that differs.  An empty PATH
is returned unchanged."
  (let ((parts (split-string (directory-file-name path) "/" t)))
    (if parts (car (last parts)) path)))

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

(defun agmon--run-duration-seconds (run now)
  "Return RUN's elapsed run time in seconds as of NOW, or nil if unknown.
Run time is `started_at' to `ended_at' once the run has ended, else to
NOW so a live run's time ticks up.  Distinct from age
\(`agmon--run-age-seconds'), which is always time-since-start: a run
that finished an hour ago keeps its true run time here but grows older
there."
  (let-alist run
    (let ((start (agmon--parse-time .started_at)))
      (when start
        (let ((end (or (agmon--parse-time .ended_at) now)))
          (max 0 (floor (float-time (time-subtract end start)))))))))

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
  '((id       :name "Id"       :width 8  :sort t)
    (status   :name "Status"   :width 11 :sort t)
    (model    :name "Model"    :width 8  :sort t)
    (cwd      :name "Cwd"      :width 20 :sort t)
    (age      :name "Age"      :width 6  :numeric t :right-align t)
    (started  :name "Started"  :width 11 :sort t)
    (runtime  :name "Runtime"  :width 8  :numeric t :right-align t)
    (cost     :name "Cost"     :width 7  :numeric t :right-align t)
    (pipeline :name "Pipeline" :width 16 :sort t :label pipeline)
    (phase    :name "Phase"    :width 11 :sort t :label phase)
    (parent   :name "Parent"   :width 8  :sort t :label parent :short-id t)
    (labels   :name "Labels"   :width 24)
    (task     :name "Task"     :width 44))
  "Registry of every run-list column, keyed by symbol.
Each entry is (KEY :name NAME :width W [:sort t] [:numeric t]
[:right-align t] [:label LKEY] [:short-id t]).  `agmon-list-columns'
selects which of these to show and in what order; `agmon--run-cell'
renders each KEY's cell.  A `:numeric' column sorts by a stashed number
\(see `agmon--numeric-sorter') rather than by its rendered text.  A
`:label' column reads the run label named LKEY (the reserved lineage
labels pipeline/phase/parent) instead of a top-level field, and
`:short-id' renders a run-id-valued label as its memorable tail.  The
generic `labels' column then omits every label already promoted to its
own column, so nothing is shown twice (see `agmon--unpromoted-labels').
A pipeline-filtered list adds `phase' automatically (see
`agmon-list-pipeline').")

(defun agmon--labels-cell (labels)
  "Render alist LABELS as a compact \"k=v,k=v\" string, keys sorted; \"\" if none."
  (if labels
      (string-join
       (mapcar (lambda (kv) (format "%s=%s" (car kv) (cdr kv)))
               (sort (copy-sequence labels)
                     (lambda (a b) (string< (symbol-name (car a))
                                            (symbol-name (car b))))))
       ",")
    ""))

(defun agmon--promoted-label-keys ()
  "Return the label keys shown as their own column in `agmon-list-columns'.
These are the LKEY of every displayed column whose spec carries a
`:label' (see `agmon--column-specs').  `agmon--unpromoted-labels'
subtracts them so the `labels' column never repeats a label that is
already promoted to a column of its own."
  (delq nil
        (mapcar (lambda (col)
                  (plist-get (alist-get col agmon--column-specs) :label))
                agmon-list-columns)))

(defun agmon--unpromoted-labels (labels)
  "Return alist LABELS with every key promoted to its own column removed.
The remainder is what the generic `labels' column shows.  See
`agmon--promoted-label-keys'."
  (let ((promoted (agmon--promoted-label-keys)))
    (seq-remove (lambda (kv) (memq (car kv) promoted)) labels)))

(defun agmon--run-cell (key run now)
  "Render the cell for column KEY of RUN, as of time NOW.
Returns a possibly-propertized string.  KEY is one of the keys in
`agmon--column-specs'.  Status carries a face; the numeric columns
carry an invisible `agmon-sort' property holding their raw value.  A
column whose spec carries `:label' reads that label from RUN (short-id
first when `:short-id'); the `labels' column shows only labels not
already promoted to a column of their own."
  (let-alist run
    (pcase key
      ('id (agmon--short-id .run_id))
      ('status (let ((s (or .effective_status "")))
                 (propertize s 'face (agmon--status-face s))))
      ('model (or .model ""))
      ('cwd (agmon--abbrev-path (or .cwd "")))
      ('age (let ((start (agmon--parse-time .started_at)))
              (propertize (agmon--format-age (agmon--run-age-seconds run now))
                          'agmon-sort (if start (float-time start) 0))))
      ('runtime (let ((secs (agmon--run-duration-seconds run now)))
                  (propertize (agmon--format-age secs) 'agmon-sort (or secs 0))))
      ('started (let ((tm (agmon--parse-time .started_at)))
                  (if tm (format-time-string "%Y-%m-%d" tm) "")))
      ('cost (propertize (agmon--format-cost .total_cost_usd)
                         'agmon-sort (if (numberp .total_cost_usd)
                                         .total_cost_usd 0)))
      ('labels (agmon--truncate
                (agmon--labels-cell (agmon--unpromoted-labels .labels)) 40))
      ('task (agmon--truncate (agmon--oneline (or .prompt_preview "")) 100))
      (_ (let* ((spec (alist-get key agmon--column-specs))
                (lkey (plist-get spec :label)))
           (if lkey
               (let ((val (or (alist-get lkey .labels) "")))
                 (if (plist-get spec :short-id) (agmon--short-id val) val))
             ""))))))

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

;; The cost rollup (GET /v1/stats/costs) is a second tabulated-list screen:
;; one row per day (date, run count, cost, turns) plus a bold totals row.
;; These builders are pure -- they turn the parsed payload into
;; `tabulated-list-entries' with no network or buffer state -- so ERT can
;; drive them with a canned alist.  Each numeric cell stashes its raw value
;; on an `agmon-sort' property, reusing `agmon--numeric-sorter' for the
;; column sorts exactly as the run list does.

(defun agmon--cost-epoch (date)
  "Return DATE, a \"YYYY-MM-DD\" string, as epoch seconds; 0 if unparseable.
Used only as the Date column's numeric sort value, so the exact epoch and
timezone do not matter -- only that it orders dates chronologically.
`date-to-time' parses leniently (a non-date fills in default fields
rather than erroring), so guard on the YYYY-MM-DD shape before trusting
it; anything else sorts as 0."
  (if (and (stringp date)
           (string-match-p "\\`[0-9]\\{4\\}-[0-9]\\{2\\}-[0-9]\\{2\\}" date))
      (or (ignore-errors (float-time (date-to-time date))) 0)
    0))

(defun agmon--cost-cell (text sort &optional face)
  "Return TEXT as a cost-table cell carrying numeric SORT and optional FACE.
SORT is stashed on an `agmon-sort' text property (read by
`agmon--numeric-sorter'); FACE, when given, propertizes the whole cell."
  (let ((s (copy-sequence text)))
    (when (> (length s) 0)
      (put-text-property 0 (length s) 'agmon-sort sort s)
      (when face (put-text-property 0 (length s) 'face face s)))
    s))

(defun agmon--cost-entries (payload)
  "Return a `tabulated-list-entries' value for the cost rollup PAYLOAD.
PAYLOAD is the parsed /v1/stats/costs alist: a `buckets' list of daily
\(bucket runs total_cost_usd total_turns) records and a `totals' summary.
Each bucket becomes a row; a final bold totals row (sort value 0 in every
column, so the default newest-first order floats it to the bottom and any
numeric re-sort sends it to an edge rather than mid-table) sums the fleet."
  (let ((rows
         (mapcar
          (lambda (b)
            (let-alist b
              (list .bucket
                    (vector
                     (agmon--cost-cell .bucket (agmon--cost-epoch .bucket))
                     (agmon--cost-cell (number-to-string .runs) .runs)
                     (agmon--cost-cell (agmon--format-cost .total_cost_usd)
                                       (if (numberp .total_cost_usd)
                                           .total_cost_usd 0))
                     (agmon--cost-cell (number-to-string .total_turns)
                                       .total_turns)))))
          (alist-get 'buckets payload))))
    (let-alist (alist-get 'totals payload)
      (append
       rows
       (list (list 'agmon-totals
                   (vector
                    (agmon--cost-cell "TOTAL" 0 'agmon-costs-total)
                    (agmon--cost-cell (number-to-string .runs) 0
                                      'agmon-costs-total)
                    (agmon--cost-cell (agmon--format-cost .total_cost_usd) 0
                                      'agmon-costs-total)
                    (agmon--cost-cell (number-to-string .total_turns) 0
                                      'agmon-costs-total))))))))

(defun agmon--costs-format ()
  "Build the `tabulated-list-format' vector for the cost rollup.
Every column sorts numerically off its cell's `agmon-sort' property, so
Date orders chronologically and the count/cost columns by value."
  (vector
   (list "Date"  12 (agmon--numeric-sorter 0))
   (list "Runs"  6  (agmon--numeric-sorter 1) :right-align t)
   (list "Cost"  10 (agmon--numeric-sorter 2) :right-align t)
   (list "Turns" 7  (agmon--numeric-sorter 3) :right-align t)))

(defun agmon--oneline (s)
  "Collapse each whitespace sequence in S to a single space, trimmed."
  (string-join (split-string (or s "") "[ \t\n\r]+" t) " "))

(defun agmon--truncate (s width)
  "Truncate S to WIDTH characters, marking the cut with an ellipsis."
  (if (and width (> (length s) width))
      (concat (substring s 0 (max 0 (1- width))) "…")
    s))

(defun agmon--nonempty (s)
  "Return S, unless it is nil or empty, in which case nil."
  (and s (not (string-empty-p s)) s))

(defun agmon--format-when (time)
  "Format Emacs TIME as an org-style local timestamp with weekday.
\"2026-07-09 Wed 21:08\".  Returns \"\" when TIME is nil."
  (if time (format-time-string "%Y-%m-%d %a %H:%M" time) ""))

(defun agmon--format-tool-counts (counts)
  "Render alist COUNTS, ((TOOL . N)...), as \"Bash 18 · Read 11\".
Returns the empty string when COUNTS is nil."
  (string-join
   (mapcar (lambda (c) (format "%s %s" (car c) (cdr c))) counts)
   " · "))

(defun agmon--md-inline (line)
  "Apply inline Markdown faces (code, then bold) to LINE.
Returns a propertized copy; unmatched text keeps its properties.  We
strip the delimiters off the whole match rather than lean on capture
groups, which sidesteps a `replace-regexp-in-string' match-group quirk."
  (let ((s line))
    (setq s (replace-regexp-in-string
             "`[^`]+`"
             (lambda (m) (propertize (substring m 1 -1) 'face 'agmon-detail-code))
             s t t))
    (setq s (replace-regexp-in-string
             "\\*\\*[^*]+?\\*\\*"
             (lambda (m) (propertize (substring m 2 -2) 'face 'bold))
             s t t))
    s))

(defun agmon--md-cells (line)
  "Split a Markdown table row LINE into a list of trimmed cell strings.
Drops the leading and trailing pipe, then splits on the interior ones."
  (let ((s (string-trim line)))
    (setq s (replace-regexp-in-string "\\`|" "" s))
    (setq s (replace-regexp-in-string "|\\'" "" s))
    (mapcar #'string-trim (split-string s "|"))))

(defun agmon--md-separator-cell-p (cell)
  "Non-nil if CELL is a table separator cell (dashes with optional colons)."
  (string-match-p "\\`:?-+:?\\'" cell))

(defun agmon--md-separator-row-p (line)
  "Non-nil if LINE is a Markdown table header separator, e.g. |---|:--:|.
Requires a pipe, so a bare \"---\" horizontal rule does not qualify."
  (and (string-search "|" line)
       (let ((cells (agmon--md-cells line)))
         (and cells (seq-every-p #'agmon--md-separator-cell-p cells)))))

(defun agmon--md-pad (cell width)
  "Right-pad rendered CELL with spaces to display WIDTH."
  (concat cell (make-string (max 0 (- width (string-width cell))) ?\s)))

(defun agmon--render-table (rows)
  "Render ROWS, raw Markdown table row strings, to an aligned table string.
Inline markup is applied to cells; cells wider than
`agmon-detail-table-cell-width' are truncated.  The header row is drawn
in the heading face above a rule; columns are separated by \" | \"."
  (let* ((grid (mapcar
                (lambda (row)
                  (mapcar (lambda (c)
                            (agmon--truncate (agmon--md-inline c)
                                             agmon-detail-table-cell-width))
                          (agmon--md-cells row)))
                (seq-remove #'agmon--md-separator-row-p rows)))
         (ncols (apply #'max 0 (mapcar #'length grid)))
         (widths (make-vector ncols 0)))
    (dolist (row grid)
      (seq-do-indexed
       (lambda (cell i) (aset widths i (max (aref widths i) (string-width cell))))
       row))
    (let ((line (lambda (cells)
                  (string-join
                   (seq-map-indexed
                    (lambda (cell i) (agmon--md-pad cell (aref widths i)))
                    cells)
                   " │ ")))
          (out nil)
          (header (car grid)))
      (when header
        (let ((h (funcall line header)))
          (add-face-text-property 0 (length h) 'agmon-detail-heading nil h)
          (push h out))
        (push (mapconcat (lambda (w) (make-string w ?─))
                         (append widths nil) "─┼─")
              out))
      (dolist (row (cdr grid))
        (push (funcall line row) out))
      (string-join (nreverse out) "\n"))))

(defun agmon--render-markdown (text)
  "Render Markdown TEXT to a propertized string.
Handles ATX headings (#...), unordered bullets, pipe tables, inline code
and bold -- enough to make result_text readable; deliberately not a full
parser."
  (let ((lines (split-string text "\n"))
        (out nil))
    (while lines
      (let ((line (car lines)))
        (cond
         ;; Table: a pipe row immediately followed by a |---| separator.
         ((and (cdr lines)
               (string-search "|" line)
               (agmon--md-separator-row-p (cadr lines)))
          (let ((block (list line)))    ; header row
            (setq lines (cddr lines))     ; skip past header and separator
            (while (and lines (string-search "|" (car lines)))
              (push (car lines) block)
              (setq lines (cdr lines)))
            ;; BLOCK is header + body rows, in order after `nreverse'.
            (push (agmon--render-table (nreverse block)) out)))
         ;; Heading.
         ((string-match "\\`#+[ \t]+\\(.*\\)\\'" line)
          (push (propertize (match-string 1 line) 'face 'agmon-detail-heading) out)
          (setq lines (cdr lines)))
         ;; Unordered bullet.
         ((string-match "\\`[ \t]*[-*][ \t]+\\(.*\\)\\'" line)
          (push (concat "  • " (agmon--md-inline (match-string 1 line))) out)
          (setq lines (cdr lines)))
         (t
          (push (agmon--md-inline line) out)
          (setq lines (cdr lines))))))
    (string-join (nreverse out) "\n")))

(defun agmon--detail-field (label value)
  "Format an aligned LABEL/VALUE row for the detail buffer.
Returns nil when VALUE is empty, so callers can `delq' it out."
  (when (agmon--nonempty value)
    (concat (propertize (format "%-9s" label) 'face 'agmon-detail-label)
            value)))

(defun agmon--lineage (run-id session-id runs)
  "Return (FROM . BY), a session's siblings split around RUN-ID.
FROM are runs in RUNS sharing SESSION-ID that started before RUN-ID (the
runs it was resumed from); BY are those that started after (the runs that
resumed it), each a run alist ordered by start time.  Nil when SESSION-ID
is nil.  Pure."
  (when session-id
    (let* ((sibs (seq-filter
                  (lambda (r) (equal (alist-get 'session_id r) session-id))
                  runs))
           (sorted (sort sibs
                         (lambda (a b)
                           (string< (or (alist-get 'started_at a) "")
                                    (or (alist-get 'started_at b) "")))))
           (seen nil) (from nil) (by nil))
      (dolist (r sorted)
        (cond ((equal (alist-get 'run_id r) run-id) (setq seen t))
              (seen (push r by))
              (t (push r from))))
      (cons (nreverse from) (nreverse by)))))

(defvar-keymap agmon--lineage-map
  :doc "Keymap for mouse activation of a detail-buffer link or toggle line."
  "<mouse-1>" #'agmon-detail-follow
  "<mouse-2>" #'agmon-detail-follow)

(defun agmon--lineage-line (label run now)
  "Format one lineage RUN as a LABEL row for the detail buffer, as of NOW.
LABEL is \"resumed from\" or \"resumed by\".  The whole row carries the
run's id as an `agmon-run-id' text property (and a mouse keymap), so
`agmon-detail-follow' -- on RET or a click -- opens that run; the id
itself wears the `link' face as an affordance."
  (let-alist run
    (let* ((status (or .effective_status ""))
           (start (agmon--parse-time .started_at))
           (line (concat
                  "  "
                  (propertize (format "%-13s" label) 'face 'agmon-detail-label)
                  (propertize (agmon--short-id .run_id) 'face 'link)
                  "  "
                  (propertize (format "%-10s" status)
                              'face (agmon--status-face status))
                  (if start
                      (propertize
                       (format "  %s ago"
                               (agmon--format-age
                                (floor (float-time (time-subtract now start)))))
                       'face 'shadow)
                    ""))))
      (propertize line
                  'agmon-run-id .run_id
                  'mouse-face 'highlight
                  'help-echo "mouse-1/RET: open this run"
                  'keymap agmon--lineage-map))))

(defun agmon--indent-field (label value &optional width)
  "An indented LABEL/VALUE row for a sub-section heading (Pipeline, Labels).
LABEL is right-padded to WIDTH columns (default 9) so sibling rows align;
pass a wider WIDTH when a block has long keys (e.g. \"experiment\") that
would otherwise crowd the value."
  (concat "  "
          (propertize (string-pad label (or width 9)) 'face 'agmon-detail-label)
          value))

(defun agmon--pipeline-value-line (pipeline)
  "Render the clickable pipeline-id row; RET/click lists that pipeline.
Carries PIPELINE as an `agmon-pipeline' text property that
`agmon-detail-follow' routes to `agmon-list-pipeline'."
  (let ((line (concat "  "
                      (propertize (format "%-9s" "pipeline") 'face 'agmon-detail-label)
                      (propertize pipeline 'face 'link))))
    (propertize line
                'agmon-pipeline pipeline
                'mouse-face 'highlight
                'help-echo "mouse-1/RET: list this pipeline"
                'keymap agmon--lineage-map)))

(defun agmon--pipeline-ref (label run-id &optional phase status)
  "Format a clickable pipeline-lineage reference row for RUN-ID.
LABEL is the relation (parent/child/sibling); PHASE and STATUS annotate a
sibling.  The row carries RUN-ID as `agmon-run-id' (with the lineage
mouse map) so `agmon-detail-follow' -- RET or a click -- opens it."
  (let ((line (concat
               "  "
               (propertize (format "%-9s" label) 'face 'agmon-detail-label)
               (propertize (agmon--short-id run-id) 'face 'link)
               (if (agmon--nonempty phase) (concat "  " phase) "")
               (if (agmon--nonempty status)
                   (concat "  " (propertize status 'face (agmon--status-face status)))
                 ""))))
    (propertize line
                'agmon-run-id run-id
                'mouse-face 'highlight
                'help-echo "mouse-1/RET: open this run"
                'keymap agmon--lineage-map)))

(defun agmon--render-summary (summary now show-issues runs)
  "Render SUMMARY, a parsed /summary payload, to a display string as of NOW.
SHOW-ISSUES non-nil expands the per-issue detail; otherwise only the
Issues heading shows.  RUNS is the run list used to derive session
lineage (nil to omit it).  Pure: it takes data and returns a propertized
string, with no network call and no buffer mutation, so ert can diff it
against a canned payload."
  (let-alist summary
    (let* ((status (or .status.effective_status ""))
           (started (agmon--parse-time .run.started_at))
           (started-age (and started
                             (floor (float-time (time-subtract now started)))))
           (dur (let ((d (or .metrics.duration_seconds
                             (and started (float-time
                                           (time-subtract now started))))))
                  (and d (floor d))))
           (git (agmon--nonempty
                 (string-trim
                  (concat (or .run.git_branch "")
                          (if .run.git_commit (concat " @ " .run.git_commit) "")))))
           (started-str
            (and started
                 (concat (agmon--format-when started)
                         (if started-age
                             (propertize
                              (format "   ·   %s ago" (agmon--format-age started-age))
                              'face 'shadow)
                           ""))))
           ;; Duration carries the run's extent -- elapsed time plus the
           ;; turn/event counts and exit code.  Turns and events measure how
           ;; much ran, not what it cost, so they live here, not on Cost;
           ;; keeping them off Cost also stops a still-running run (no cost
           ;; yet) from showing a lone "N events" under a Cost label.
           (dur-str (string-join
                     (delq nil
                           (list (and dur (agmon--format-age dur))
                                 (and .run.num_turns
                                      (format "%d turns" .run.num_turns))
                                 (and .run.event_count
                                      (format "%d events" .run.event_count))
                                 (and (numberp .run.exit_code)
                                      (format "exit %d" .run.exit_code))))
                     "   ·   "))
           (cost-str (agmon--nonempty (agmon--format-cost .run.total_cost_usd)))
           (tools (agmon--format-tool-counts .metrics.tool_counts))
           (lines nil))
      ;; Header: a status-coloured dot, the short id, and the status word.
      (push (concat (propertize "● " 'face (agmon--status-face status))
                    (propertize (agmon--short-id .run.run_id) 'face 'bold)
                    "   "
                    (propertize status 'face (agmon--status-face status)))
            lines)
      (push "" lines)
      ;; Aligned label/value block -- one field per line, labels dimmed.
      ;; The full run id leads (the header only shows the short id).
      (dolist (field (delq nil
                           (list (agmon--detail-field "Run" .run.run_id)
                                 (agmon--detail-field "Path" .run.cwd)
                                 (agmon--detail-field "Git" git)
                                 (agmon--detail-field "Host" .run.host)
                                 (agmon--detail-field "Model" .run.model)
                                 (agmon--detail-field "Started" started-str)
                                 (agmon--detail-field "Duration" dur-str)
                                 (agmon--detail-field "Cost" cost-str)
                                 (agmon--detail-field "Tools" tools))))
        (push field lines))
      ;; Activity: latest progress line and last tool call, same label style.
      (when (or (agmon--nonempty .activity.progress) .activity.last_tool)
        (push "" lines))
      (when (agmon--nonempty .activity.progress)
        (push (agmon--detail-field "Progress" (agmon--oneline .activity.progress))
              lines))
      (when .activity.last_tool
        (let ((lt .activity.last_tool))
          (push (agmon--detail-field
                 "Last"
                 (concat (or (alist-get 'tool lt) "") "  "
                         (agmon--truncate
                          (agmon--oneline (or (alist-get 'target lt) "")) 70)))
                lines)))
      ;; Pipeline lineage: the intentional relation from reserved labels
      ;; (pipeline/phase/parent + derived children/siblings).  Deliberately
      ;; kept distinct from the session resume chain below -- never conflate.
      (when .lineage
        (push "" lines)
        (push (propertize "Pipeline" 'face 'agmon-detail-heading) lines)
        (when (agmon--nonempty .lineage.pipeline)
          (push (agmon--pipeline-value-line .lineage.pipeline) lines))
        (when (agmon--nonempty .lineage.phase)
          (push (agmon--indent-field "phase" .lineage.phase) lines))
        (when (agmon--nonempty .lineage.parent)
          (push (agmon--pipeline-ref "parent" .lineage.parent) lines))
        (dolist (c .lineage.children)
          (push (agmon--pipeline-ref "child" c) lines))
        ;; Siblings already surfaced as parent/child would just repeat.
        (let ((shown (append (and .lineage.parent (list .lineage.parent))
                             .lineage.children)))
          (dolist (s .lineage.siblings)
            (let ((rid (alist-get 'run_id s)))
              (unless (member rid shown)
                (push (agmon--pipeline-ref "sibling" rid
                                           (alist-get 'phase s)
                                           (alist-get 'effective_status s))
                      lines))))))
      ;; Session lineage: other runs sharing this run's session_id (resumes).
      (let* ((lin (agmon--lineage .run.run_id .run.session_id runs))
             (from (car lin))
             (by (cdr lin)))
        (when (or from by)
          (push "" lines)
          (push (propertize "Session" 'face 'agmon-detail-heading) lines)
          (dolist (r from) (push (agmon--lineage-line "resumed from" r now) lines))
          (dolist (r by) (push (agmon--lineage-line "resumed by" r now) lines))))
      ;; Labels: any non-reserved labels, raw (the reserved ones surface
      ;; above as Pipeline).
      (let ((other (seq-remove (lambda (kv) (memq (car kv) '(pipeline phase parent)))
                               .run.labels)))
        (when other
          (push "" lines)
          (push (propertize "Labels" 'face 'agmon-detail-heading) lines)
          ;; Align the block to its widest key (min 9, +2 gutter) so a long
          ;; key like "experiment" does not crowd its value.
          (let ((w (max 9 (+ 2 (apply #'max 0
                                      (mapcar (lambda (kv)
                                                (length (format "%s" (car kv))))
                                              other))))))
            (dolist (kv other)
              (push (agmon--indent-field (format "%s" (car kv))
                                         (format "%s" (cdr kv))
                                         w)
                    lines)))))
      ;; Issues, collapsed to the heading unless SHOW-ISSUES (they are
      ;; usually the routine \"read before edit\" kind, so hide by default).
      (when .issues
        (push "" lines)
        (push (propertize
               (concat (if show-issues "▾ " "▸ ")
                       (propertize (format "Issues (%d)" (length .issues))
                                   'face 'agmon-detail-heading))
               ;; RET (or a click) anywhere on the heading toggles the section
               ;; -- same "activate the thing at point" verb as following a
               ;; link; the marker signals foldability, as the tail's does.
               'agmon-toggle 'issues
               'keymap agmon--lineage-map)
              lines)
        (when show-issues
          (dolist (iss .issues)
            (let ((cat (or (alist-get 'category iss) ""))
                  (tool (or (alist-get 'tool iss) ""))
                  (seq (alist-get 'seq iss))
                  (snip (or (alist-get 'snippet iss) "")))
              (push (concat "  "
                            (propertize (format "[%s]" cat)
                                        'face 'agmon-status-error)
                            " " tool (if seq (format " @%s" seq) ""))
                    lines)
              (push (concat "    " (agmon--truncate (agmon--oneline snip) 90))
                    lines)))))
      ;; Result: the final message, Markdown-styled, in full.
      (push "" lines)
      (push (propertize "Result" 'face 'agmon-detail-heading) lines)
      (let ((result (agmon--nonempty (string-trim (or .result_text "")))))
        (push (if result (agmon--render-markdown result) "(no result yet)")
              lines))
      (concat (string-join (nreverse lines) "\n") "\n"))))

;;;; Run list mode

(define-derived-mode agmon-list-mode tabulated-list-mode "Agmon"
  "Major mode for browsing the agmon run fleet.

Each row is one run; the list auto-refreshes while a window shows it.
RET opens the run at point, t tails it live, J shows its raw JSON, and
$ opens the fleet cost rollup.  g refreshes now, q buries the buffer,
and ? shows this help.

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
  ;; Stop polling when this buffer dies; start the visibility-gated timer.
  (add-hook 'kill-buffer-hook #'agmon--list-cleanup nil t)
  (tabulated-list-init-header)
  (agmon--list-start-timer))

(defun agmon-sort-default ()
  "Restore the default newest-first sort in an agmon list buffer."
  (interactive)
  (setq tabulated-list-sort-key (agmon--default-sort-key))
  (tabulated-list-init-header)
  (tabulated-list-print t))

;; `o' restores the default sort after a column-header click sends you
;; elsewhere; plain tabulated-list-mode has no key to undo a sort.
(keymap-set agmon-list-mode-map "o" #'agmon-sort-default)

;; `RET' drills into the run at point.  (evil users: this key is only
;; reachable because agmon-list-mode-map is marked as an overriding map
;; in the operator's config; see the package README.)
(keymap-set agmon-list-mode-map "RET" #'agmon-show-run)

;; `J' shows the raw /summary JSON for the run at point.
(keymap-set agmon-list-mode-map "J" #'agmon-show-json)

;; `t' opens a live event tail for the run at point.
(keymap-set agmon-list-mode-map "t" #'agmon-tail)

;; `$' opens the fleet-wide cost rollup (a separate screen, not per-run).
(keymap-set agmon-list-mode-map "$" #'agmon-costs)

;; `?' shows the mode help (the standard `C-h m' view); handy in a
;; read-only buffer where `?' is otherwise unused.
(keymap-set agmon-list-mode-map "?" #'describe-mode)

;; Reserved, intentionally unbound: `k' (kill), `r' (resume), `d'
;; (dispatch) belong to run control, which stage 4 owns.  Leave them
;; free here so those verbs land consistently across the fleet later.

(defvar-local agmon--list-labels nil
  "Label filters (\"key=value\" strings) active in this list buffer, or nil.
Threaded into `agmon--runs' by both refresh paths so the buffer shows
only matching runs; set by `agmon-list-pipeline'.")

(defun agmon--list-refresh ()
  "Re-fetch the run list into `tabulated-list-entries', synchronously.
Installed on `tabulated-list-revert-hook', so `g' refreshes now (blocking
on a manual refresh is fine); the timer path uses `agmon--list-auto-refresh'.
Honors this buffer's `agmon--list-labels' filter."
  (setq tabulated-list-entries
        (agmon--run-entries (agmon--runs nil nil nil agmon--list-labels))))

;; Auto-refresh: a repeating timer re-fetches the list, but the callback
;; runs only when a window actually shows the buffer -- so a buried list
;; never chatters on the network -- and the fetch is async so Emacs never
;; blocks on it.  The timer is buffer-local and cancelled on
;; `kill-buffer-hook', which is what makes killing the buffer provably
;; stop all polling (`M-x list-timers').

(defvar-local agmon--list-timer nil
  "This list buffer's repeating auto-refresh timer, or nil.
Cancelled on `kill-buffer-hook' so a killed buffer stops polling.")

(defun agmon--list-auto-refresh (buffer)
  "Async-refresh the run list in BUFFER, but only while it is visible.
The visibility gate means a buried list issues no background requests.
Point (kept on the same run id) and the sort survive the reprint; on a
request failure the last good list stays put."
  (when (and (buffer-live-p buffer) (get-buffer-window buffer 'visible))
    (agmon--runs
     nil nil
     (lambda (runs)
       ;; The buffer may have been killed while the request was in flight.
       (when (buffer-live-p buffer)
         (with-current-buffer buffer
           (setq tabulated-list-entries (agmon--run-entries runs))
           (tabulated-list-print t))))   ; t: keep point-on-id and sort
     (buffer-local-value 'agmon--list-labels buffer))))

(defun agmon--list-start-timer ()
  "Start this list buffer's repeating auto-refresh timer.
Does nothing when `agmon-list-refresh-seconds' is zero."
  (when agmon--list-timer (cancel-timer agmon--list-timer))
  (setq agmon--list-timer nil)
  (when (> agmon-list-refresh-seconds 0)
    (let ((timer (run-with-timer agmon-list-refresh-seconds
                                 agmon-list-refresh-seconds
                                 #'agmon--list-auto-refresh
                                 (current-buffer))))
      (setq agmon--list-timer timer)
      (push timer agmon--timers))))

(defun agmon--list-cleanup ()
  "Cancel this buffer's auto-refresh timer.
Installed on `kill-buffer-hook', so killing the buffer stops polling."
  (when agmon--list-timer
    (cancel-timer agmon--list-timer)
    (setq agmon--timers (delq agmon--list-timer agmon--timers))
    (setq agmon--list-timer nil)))

;;;; Run detail buffer
;;
;; RET on a list row opens a read-only buffer describing one run, drawn
;; from GET /summary.  It derives from `special-mode' -- Emacs's base for
;; non-editable, command-driven buffers (Help, dired-like views): it makes
;; the buffer read-only and binds `q' to bury it and `g' to revert.  We
;; hang our re-fetch on the revert machinery, so `g' refreshes for free,
;; exactly as the list does.

(defvar-local agmon--detail-run-id nil
  "Full run id this detail buffer describes.
Buffer-local: each *agmon: ID* buffer remembers its own run, so `g'
knows what to re-fetch.")

(defvar-local agmon--detail-summary nil
  "Cached parsed /summary for this detail buffer.
`g' refreshes it from the server; the `RET' issues toggle re-renders
from this cache without a network round-trip.")

(defvar-local agmon--detail-show-issues nil
  "Non-nil when this buffer expands the per-issue detail.
Buffer-local and off by default -- issues are usually routine.  `RET'
on the Issues heading flips it.")

(defvar-local agmon--detail-runs nil
  "Cached run list for this detail buffer, used to derive session lineage.
Fetched alongside the summary; a re-render (e.g. the `RET' toggle) reuses
it without another round-trip.")

;; `q' (bury) and `g' (revert) are inherited from `special-mode'.  RET
;; both follows the link and folds the Issues section at point (see
;; `agmon-detail-follow'), so it is the single fold verb -- no TAB
;; binding, which leaves TAB free for navigation (evil users especially,
;; whose normal state binds nothing to Tab, so a binding here would win).
(defvar-keymap agmon-detail-mode-map
  :doc "Keymap for `agmon-detail-mode'."
  "J" #'agmon-show-json
  "RET" #'agmon-detail-follow
  "t" #'agmon-tail
  "?" #'describe-mode)

(define-derived-mode agmon-detail-mode special-mode "Agmon-Detail"
  "Major mode for a single run's detail view.

RET acts on whatever is at point -- follows a lineage/pipeline link, or
folds the Issues section when point is on its heading (the ▸/▾ marker).
t tails this run, J shows its raw JSON, g refreshes, q buries, and ?
shows this help.

\\{agmon-detail-mode-map}"
  ;; `g' runs `revert-buffer', which delegates to this function; we
  ;; re-fetch and redraw.  Same pattern as the list's revert hook.
  (setq-local revert-buffer-function #'agmon--detail-revert)
  ;; Soft-wrap long prose (the result text) at the window edge on word
  ;; boundaries, so the operator never has to toggle wrapping by hand.
  (visual-line-mode 1))

(defun agmon--detail-revert (&rest _)
  "Re-fetch this buffer's summary from the server and redraw.
Installed as the buffer-local `revert-buffer-function', so `g' works.
Ignores its arguments."
  (agmon--detail-fetch))

(defun agmon--detail-fetch ()
  "Fetch this buffer's run summary and the run list, then redraw.
The run list feeds the session-lineage section."
  (setq agmon--detail-summary (agmon--summary agmon--detail-run-id))
  (setq agmon--detail-runs (agmon--runs))
  (agmon--detail-render))

(defun agmon--detail-render ()
  "Redraw this buffer from its cached summary -- no network.
`special-mode' keeps the buffer read-only; we lift that with
`inhibit-read-only' only for our own insert, and try to keep point so a
`TAB' toggle does not jump the view."
  (let ((inhibit-read-only t)
        (p (point)))
    (erase-buffer)
    (insert (agmon--render-summary agmon--detail-summary
                                   (current-time)
                                   agmon--detail-show-issues
                                   agmon--detail-runs))
    (goto-char (min p (point-max)))))

(defun agmon-detail-toggle-issues ()
  "Expand or collapse the per-issue detail in this run's detail buffer."
  (interactive)
  (setq agmon--detail-show-issues (not agmon--detail-show-issues))
  (agmon--detail-render))

(defun agmon-detail-follow (&optional event)
  "Follow the link or toggle the section at point.
On a mouse EVENT use the click position, otherwise point.  A run id rides
on the line as an `agmon-run-id' property (opens its detail); a pipeline
id as `agmon-pipeline' (opens `agmon-list-pipeline'); the Issues heading
carries `agmon-toggle' (expands or collapses that section).  Bound to RET
and to a mouse click on those lines."
  (interactive (list last-nonmenu-event))
  (let* ((win (if (mouse-event-p event) (posn-window (event-end event))
                (selected-window)))
         (pos (if (mouse-event-p event) (posn-point (event-end event)) (point)))
         (id (get-text-property pos 'agmon-run-id))
         (pipeline (get-text-property pos 'agmon-pipeline))
         (toggle (get-text-property pos 'agmon-toggle)))
    (cond
     ;; Reuse the current window: a plain window follows same-window, but a
     ;; side window is dedicated -- reopen in its side slot so it reuses that
     ;; window instead of spawning a new one.
     (id (agmon--open-detail id (and (window-parameter win 'window-side) t)))
     (pipeline (agmon-list-pipeline pipeline))
     ((eq toggle 'issues) (agmon-detail-toggle-issues))
     (t (user-error "Nothing to follow or toggle at point")))))

(defun agmon--open-detail (run-id &optional side)
  "Open, refresh, and select the detail buffer for RUN-ID.
With SIDE non-nil, show it in a right-hand side window instead of the
current one."
  (let ((buf (get-buffer-create (format "*agmon: %s*" (agmon--short-id run-id)))))
    (with-current-buffer buf
      (agmon-detail-mode)
      (setq agmon--detail-run-id run-id)
      (agmon--detail-fetch))
    (if side
        (select-window
         (display-buffer buf '((display-buffer-in-side-window)
                               (side . right)
                               (window-width . 0.5))))
      (pop-to-buffer-same-window buf))))

(defun agmon-show-run (&optional run-id side)
  "Open the detail buffer for RUN-ID, defaulting to the run at point.
With a prefix argument (interactively, SIDE non-nil), open it in a
right-hand side window so the list stays visible."
  (interactive (list nil current-prefix-arg))
  (let ((id (or run-id (tabulated-list-get-id))))
    (unless id (user-error "No run at point"))
    (agmon--open-detail id side)))

;;;; Raw-JSON escape hatch
;;
;; `J' anywhere in agmon shows the run at point's /summary as
;; pretty-printed JSON -- the CLI's `--json' reborn as a keybinding, for
;; when the rendered view hides something you need to see.  Highlighting
;; comes from `json-ts-mode' when the optional tree-sitter JSON grammar
;; is installed, else from `js-json-mode' (regexp-based, always
;; available); either way the mode adds a read-only, `q'-to-bury view.
;; Since neither parent is a `special-mode', evil users bind `q'/`gr'
;; explicitly (see the README).

(defvar-local agmon--json-run-id nil
  "Run id whose raw /summary this JSON buffer shows.
Buffer-local, so `g' knows what to re-fetch.")

(defvar-keymap agmon-json-mode-map
  :doc "Keymap for `agmon-json-mode'."
  "q" #'quit-window
  "g" #'revert-buffer
  "?" #'describe-mode)

(defun agmon--json-parent-mode ()
  "Enable the best available JSON major mode in the current buffer.
`json-ts-mode' when the tree-sitter JSON grammar is installed (an
optional extra -- see the README), otherwise `js-json-mode'.  Decided
per call, so a grammar installed mid-session is picked up by the next
JSON buffer without reloading agmon."
  (if (treesit-ready-p 'json t) (json-ts-mode) (js-json-mode)))

(define-derived-mode agmon-json-mode agmon--json-parent-mode "Agmon-JSON"
  "Major mode for agmon's raw-JSON escape hatch.
A read-only, syntax-highlighted view of a run's /summary payload; `q'
buries it, `g' re-fetches, and `?' shows this help."
  ;; Advertise which backend won; \"[ts]\" also confirms a grammar install.
  (when (treesit-ready-p 'json t)
    (setq mode-name "Agmon-JSON[ts]"))
  (setq buffer-read-only t)
  (setq-local revert-buffer-function #'agmon--json-revert))

(defun agmon--json-revert (&rest _)
  "Re-fetch and redraw this JSON buffer.
Installed as the buffer-local `revert-buffer-function'.  Ignores its
arguments."
  (agmon--json-render))

(defun agmon--json-render ()
  "Fetch this buffer's run summary as raw JSON and pretty-print it."
  (let ((raw (agmon--request
              (format "/v1/runs/%s/summary" agmon--json-run-id) t))
        (inhibit-read-only t))
    (erase-buffer)
    (insert raw)
    (json-pretty-print-buffer)
    (goto-char (point-min))))

(defun agmon--run-id-at-point ()
  "Return the run id the current buffer or point refers to, or nil.
A detail buffer answers with its own run; the list answers with the
row at point."
  (or agmon--detail-run-id
      (and (derived-mode-p 'tabulated-list-mode) (tabulated-list-get-id))))

(defun agmon-show-json ()
  "Show the pretty-printed raw /summary JSON for the run at point.
Works from the run list (the row at point) and from a detail buffer
\(its run).  The buffer is read-only; `q' buries it."
  (interactive)
  (let ((id (agmon--run-id-at-point)))
    (unless id (user-error "No run at point"))
    (let ((buf (get-buffer-create
                (format "*agmon-json: %s*" (agmon--short-id id)))))
      (with-current-buffer buf
        (agmon-json-mode)
        (setq agmon--json-run-id id)
        (agmon--json-render))
      (pop-to-buffer buf))))

;;;; Jump to a run
;;
;; `agmon-jump' is a completing-read over every run, openable from any
;; buffer.  It supersedes the CLI's substring id resolver: rather than
;; typing a fragment and hoping it is unambiguous, you narrow the live
;; candidate list with your completion UI (Vertico, etc.) and pick.

(defun agmon--jump-candidate (run)
  "Format RUN as an `agmon-jump' completion candidate.
Shows the short id, the status (in its status face), the abbreviated
cwd, and a truncated task preview -- the preview is what tells two
same-repo runs apart, so the candidate list scans like a compact run
list."
  (let-alist run
    (let ((status (or .effective_status "")))
      (format "%-8s  %s  %-20s  %s"
              (agmon--short-id .run_id)
              (propertize (format "%-11s" status)
                          'face (agmon--status-face status))
              (agmon--truncate (agmon--abbrev-path (or .cwd "")) 20)
              (agmon--truncate (agmon--oneline (or .prompt_preview "")) 60)))))

;;;###autoload
(defun agmon-jump (&optional side)
  "Pick a run by completion and open its detail buffer, from anywhere.
Candidates show each run's id, status, and cwd.  With a prefix argument
\(SIDE non-nil) open the detail in a side window.  Unlike the CLI's
substring id matching, this offers every run for interactive narrowing."
  (interactive "P")
  (let* ((runs (agmon--runs))
         (candidates (mapcar (lambda (run)
                               (cons (agmon--jump-candidate run)
                                     (alist-get 'run_id run)))
                             runs))
         (choice (and candidates
                      (completing-read "Run: " candidates nil t))))
    (unless choice (user-error "No runs to jump to"))
    (agmon--open-detail (cdr (assoc choice candidates)) side)))

;;;; Event-tail rendering (pure)
;;
;; These port the CLI's editorial event line (agmon/render.py
;; `summarize_event' and the agmon/derive.py helpers it leans on) to
;; Elisp.  They are pure -- an event alist in, a (TEXT . FACE) cons or a
;; string out -- so ert can diff them against canned events, and the
;; tail buffer below just inserts what they return.

(defun agmon--event-type (event)
  "Return EVENT's type, falling back to its payload's type."
  (or (alist-get 'type event)
      (let ((p (alist-get 'payload event)))
        (and (listp p) (alist-get 'type p)))))

(defun agmon--event-blocks (event)
  "Return EVENT's message content blocks (each an alist), or nil."
  (let* ((p (alist-get 'payload event))
         (msg (and (listp p) (alist-get 'message p)))
         (content (and (listp msg) (alist-get 'content msg))))
    (and (listp content) (seq-filter #'listp content))))

(defun agmon--event-text-blocks (event)
  "Return the text of every text block in EVENT, in order.
A bare string content counts as one block."
  (let* ((p (alist-get 'payload event))
         (msg (and (listp p) (alist-get 'message p)))
         (content (and (listp msg) (alist-get 'content msg))))
    (cond
     ((stringp content) (list content))
     ((listp content)
      (delq nil
            (mapcar (lambda (b)
                      (and (listp b)
                           (equal (alist-get 'type b) "text")
                           (stringp (alist-get 'text b))
                           (alist-get 'text b)))
                    content)))
     (t nil))))

(defun agmon--content-to-text (content)
  "Flatten a tool_result/result CONTENT into a plain string."
  (cond
   ((stringp content) content)
   ((listp content)
    (string-join
     (delq nil
           (mapcar (lambda (b)
                     (cond
                      ((and (listp b) (stringp (alist-get 'text b)))
                       (alist-get 'text b))
                      ((and (listp b) (stringp (alist-get 'content b)))
                       (alist-get 'content b))
                      ((stringp b) b)))
                   content))
     "\n"))
   (t "")))

(defun agmon--tool-target (input)
  "Return a display target string from a tool_use INPUT alist, capped at 120."
  (when (listp input)
    (let ((target (cond
                   ((stringp (alist-get 'file_path input)) (alist-get 'file_path input))
                   ((stringp (alist-get 'command input)) (alist-get 'command input))
                   (t (seq-find #'stringp (mapcar #'cdr input))))))
      (when (stringp target)
        (substring target 0 (min 120 (length target)))))))

(defun agmon--event-progress (event)
  "Return the last PROGRESS line in EVENT's first text block with one, or nil."
  (catch 'found
    (dolist (text (agmon--event-text-blocks event) nil)
      (let ((start 0) (last nil))
        (while (string-match "^PROGRESS: \\(.+\\)$" text start)
          (setq last (match-string 1 text)
                start (match-end 0)))
        (when last (throw 'found last))))))

(defun agmon--summarize-event (event)
  "Return (TEXT . FACE) summarizing EVENT -- the ported CLI editorial line."
  (let ((etype (agmon--event-type event))
        (payload (let ((p (alist-get 'payload event))) (and (listp p) p))))
    (pcase etype
      ("_unparseable" (cons "<unparseable line>" 'error))
      ("system"
       (cons (format "system: %s"
                     (or (alist-get 'subtype event)
                         (alist-get 'subtype payload)
                         "system"))
             'shadow))
      ("result"
       (let* ((sub (or (alist-get 'subtype payload) (alist-get 'subtype event) "?"))
              (ok (and (equal sub "success")
                       (not (eq (alist-get 'is_error payload) t))))
              (cost (alist-get 'total_cost_usd payload))
              (turns (alist-get 'num_turns payload))
              (bits (list (format "result: %s" sub))))
         (when (numberp cost)
           (setq bits (append bits (list (agmon--format-cost cost)))))
         (when turns
           (setq bits (append bits (list (format "%s turns" turns)))))
         (cons (string-join bits " · ") (if ok 'success 'error))))
      ("assistant"
       (let ((progress (agmon--event-progress event)))
         (if progress
             (cons (format "PROGRESS: %s" progress) 'agmon-tail-progress)
           (let ((tools (seq-filter (lambda (b) (equal (alist-get 'type b) "tool_use"))
                                    (agmon--event-blocks event))))
             (if tools
                 (let* ((b0 (car tools))
                        (name (or (alist-get 'name b0) "tool"))
                        (target (or (agmon--tool-target (alist-get 'input b0)) ""))
                        (extra (if (> (length tools) 1)
                                   (format " (+%d more)" (1- (length tools))) "")))
                   (cons (concat (agmon--oneline (format "→ %s %s" name target)) extra)
                         'shadow))
               (let ((texts (agmon--event-text-blocks event)))
                 (cons (or (and texts (agmon--nonempty
                                       (agmon--oneline (car (last texts)))))
                           "assistant")
                       'shadow)))))))
      ("user"
       (let* ((results (seq-filter (lambda (b) (equal (alist-get 'type b) "tool_result"))
                                   (agmon--event-blocks event)))
              (errored (seq-filter (lambda (b) (eq (alist-get 'is_error b) t)) results)))
         (cond
          (errored
           (let ((snip (agmon--oneline
                        (agmon--content-to-text (alist-get 'content (car errored))))))
             (cons (if (agmon--nonempty snip) (format "error: %s" snip) "error") 'error)))
          (results
           (let ((snip (agmon--oneline
                        (agmon--content-to-text (alist-get 'content (car results))))))
             (cons (if (agmon--nonempty snip) (format "tool_result: %s" snip) "tool_result")
                   'shadow)))
          (t (let ((texts (agmon--event-text-blocks event)))
               (cons (or (and texts (agmon--nonempty
                                     (agmon--oneline (car (last texts)))))
                         "user")
                     'shadow))))))
      (_ (cons (format "%s" (or etype (alist-get 'type event) "?")) 'shadow)))))

(defun agmon--tail-render-line (event)
  "Render EVENT as a propertized one-line tail summary (no filtering).
The line is truncated to `agmon-tail-line-width'."
  (let* ((s (agmon--summarize-event event))
         (width (and (> agmon-tail-line-width 0) agmon-tail-line-width)))
    (propertize (agmon--truncate (car s) width) 'face (cdr s))))

(defun agmon--assistant-empty-p (event)
  "Non-nil if EVENT is an assistant turn with no text, tool_use, or PROGRESS.
These carry only thinking, whose body is empty in the spool, so the tail
curates them out unless showing everything."
  (and (equal (agmon--event-type event) "assistant")
       (not (agmon--event-progress event))
       (not (seq-find (lambda (b) (equal (alist-get 'type b) "tool_use"))
                      (agmon--event-blocks event)))
       (not (seq-find (lambda (s) (agmon--nonempty (agmon--oneline s)))
                      (agmon--event-text-blocks event)))))

(defun agmon--tail-low-value-p (event)
  "Non-nil if EVENT is curated out of the tail by default.
True for a type in `agmon-tail-hidden-types' and for a thinking-only
assistant turn (see `agmon--assistant-empty-p')."
  (or (member (agmon--event-type event) agmon-tail-hidden-types)
      (agmon--assistant-empty-p event)))

(defun agmon--tail-prose (event)
  "Return EVENT's assistant prose text if it is a plain prose turn, else nil.
A prose turn is an assistant message with a non-empty text block but no
PROGRESS line and no tool call -- the kind worth folding open."
  (and (equal (agmon--event-type event) "assistant")
       (not (agmon--event-progress event))
       (not (seq-find (lambda (b) (equal (alist-get 'type b) "tool_use"))
                      (agmon--event-blocks event)))
       (let ((texts (agmon--event-text-blocks event)))
         (and texts (agmon--nonempty (string-trim (car (last texts))))))))

(defun agmon--tail-indent (text)
  "Indent every line of TEXT by four spaces for the folded-open body."
  (mapconcat (lambda (l) (concat "    " l)) (split-string text "\n") "\n"))

(defun agmon--tail-summary-line (status metrics)
  "Render the tail's terminal verdict line from STATUS and METRICS."
  (let* ((eff (or (alist-get 'effective_status status) "?"))
         (cost (alist-get 'total_cost_usd metrics))
         (dur (alist-get 'duration_seconds metrics))
         (bits (list eff)))
    (when (numberp cost)
      (setq bits (append bits (list (agmon--format-cost cost)))))
    (when (numberp dur)
      (setq bits (append bits (list (agmon--format-age (floor dur))))))
    (propertize (concat "── " (string-join bits " · "))
                'face (agmon--status-face eff))))

(defun agmon--tail-heartbeat (stalled-seconds)
  "Render a stall heartbeat line for STALLED-SECONDS."
  (propertize (format "⏳ stalled for %ss" (or stalled-seconds "?")) 'face 'warning))

;;;; Event-tail buffer
;;
;; `t' on a run opens *agmon-tail:<id>*, which cursor-polls
;; /events?after=<seq> using the returned next_after, appends the newly
;; rendered lines, and repeats on a short timer -- under the same
;; visibility discipline as the list.  The poll self-schedules (one-shot
;; timer per round) so rounds never overlap; it stops for good at a
;; terminal status.

(defconst agmon--tail-terminal-statuses '("finished" "error" "interrupted" "died")
  "Effective statuses at which the tail stops polling.")

(defvar-local agmon--tail-run-id nil "Run id this tail follows.")
(defvar-local agmon--tail-cursor 0 "Last event seq consumed (the poll cursor).")
(defvar-local agmon--tail-timer nil "This tail's pending one-shot poll timer.")
(defvar-local agmon--tail-terminal nil "Non-nil once the run reached a terminal status.")
(defvar-local agmon--tail-show-all nil "Non-nil to show `agmon-tail-hidden-types' too.")
(defvar-local agmon--tail-stalled-shown nil "Non-nil once a stall heartbeat was printed.")

;; Bind both TAB spellings: `<tab>' is what a graphical frame's Tab key
;; sends, `TAB' the terminal (C-i) fallback.
(defvar-keymap agmon-tail-mode-map
  :doc "Keymap for `agmon-tail-mode'."
  "a" #'agmon-tail-toggle-all
  "TAB" #'agmon-tail-toggle-line
  "<tab>" #'agmon-tail-toggle-line
  "?" #'describe-mode)

(define-derived-mode agmon-tail-mode special-mode "Agmon-Tail"
  "Major mode for a live event tail of one run.

Each line is one event, rendered as an editorial summary; the tail polls
while visible and stops at a terminal status.  TAB folds the assistant
prose on the line at point (the ▸/▾ marker), a toggles the low-value
event types, g refreshes, q buries, and ? shows this help.

\\{agmon-tail-mode-map}"
  (setq-local revert-buffer-function #'agmon--tail-revert)
  (visual-line-mode 1)
  ;; Prose bodies fold via overlays whose `invisible' property is this
  ;; symbol; registering it once lets each overlay hide/show on its own.
  (add-to-invisibility-spec 'agmon-prose)
  (add-hook 'kill-buffer-hook #'agmon--tail-cancel-timer nil t))

(defun agmon--tail-cancel-timer ()
  "Cancel this tail's pending poll timer.
On `kill-buffer-hook', so killing the buffer provably stops polling."
  (when agmon--tail-timer
    (cancel-timer agmon--tail-timer)
    (setq agmon--timers (delq agmon--tail-timer agmon--timers))
    (setq agmon--tail-timer nil)))

(defun agmon--tail-schedule (buffer)
  "Schedule BUFFER's next poll one `agmon-tail-poll-seconds' from now."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (agmon--tail-cancel-timer)
      (when (> agmon-tail-poll-seconds 0)
        (setq agmon--tail-timer
              (run-with-timer agmon-tail-poll-seconds nil
                              #'agmon--tail-poll buffer))
        (push agmon--tail-timer agmon--timers)))))

(defmacro agmon--tail-following (&rest body)
  "Run BODY, preserving the buffer's bottom-follow.
BODY should insert at the end of the buffer.  Any window parked at the
end, and point if it was at the end, stays pinned to the new bottom; a
window scrolled up is left alone."
  (declare (indent 0) (debug t))
  `(let ((inhibit-read-only t)
         (at-point-end (>= (point) (point-max)))
         (follow (seq-filter (lambda (w) (>= (window-point w) (point-max)))
                             (get-buffer-window-list (current-buffer) nil t))))
     (save-excursion (goto-char (point-max)) ,@body)
     (dolist (w follow) (set-window-point w (point-max)))
     (when at-point-end (goto-char (point-max)))))

(defun agmon--tail-insert (strings)
  "Append STRINGS (already-rendered lines) at the end, following the bottom."
  (when strings
    (agmon--tail-following
      (insert (mapconcat #'identity strings "\n") "\n"))))

(defun agmon--tail-append-events (events show-all)
  "Render and append EVENTS, following the bottom; SHOW-ALL bypasses the filter."
  (when events
    (agmon--tail-following
      (dolist (e events) (agmon--tail-insert-one e show-all)))))

(defun agmon--tail-insert-one (event show-all)
  "Insert EVENT's tail representation at point, or nothing if filtered.
Unless SHOW-ALL, low-value events (`agmon--tail-low-value-p') are
dropped.  Assistant prose is inserted collapsed to one line, its full
text held in an invisible overlay that `agmon-tail-toggle-line' folds open."
  (unless (and (not show-all) (agmon--tail-low-value-p event))
    (let ((prose (agmon--tail-prose event)))
      (if prose
          (agmon--tail-insert-prose prose)
        (insert (agmon--tail-render-line event) "\n")))))

(defun agmon--tail-fold-marker (collapsed)
  "Return the fold marker string: a closed triangle when COLLAPSED, else open."
  (propertize (if collapsed "▸ " "▾ ") 'face 'agmon-tail-progress))

(defun agmon--tail-insert-prose (full)
  "Insert assistant prose FULL collapsed to one line, foldable open with TAB.
When there is more than the one-line summary shows, the full text goes in
below inside an overlay hidden via the `agmon-prose' invisibility spec, a
`▸'/`▾' marker leads the summary line (an overlay `before-string'), and
the summary carries the body overlay on an `agmon-prose-ov' property.
Short prose that fits on its line is inserted plain, with no marker."
  (let* ((width (and (> agmon-tail-line-width 0) agmon-tail-line-width))
         (oneline (agmon--oneline full))
         (summary (agmon--truncate oneline width))
         (expandable (or (string-search "\n" (string-trim full))
                         (and width (> (length oneline) width))))
         (start (point)))
    (if (not expandable)
        (insert (propertize summary 'face 'shadow) "\n")
      (insert (propertize summary 'face 'shadow
                          'mouse-face 'highlight
                          'help-echo "TAB: fold open/closed")
              "\n")
      (let ((body-start (point)))
        (insert (propertize (agmon--tail-indent full) 'face 'shadow) "\n")
        (let ((body (make-overlay body-start (point)))
              (mark (make-overlay start (1- body-start))))
          (overlay-put body 'invisible 'agmon-prose)
          (overlay-put body 'agmon-mark mark)
          (overlay-put mark 'before-string (agmon--tail-fold-marker t))
          (put-text-property start (point) 'agmon-prose-ov body))))))

(defun agmon--tail-poll (buffer)
  "One poll round for BUFFER: fetch events after the cursor and consume them.
Fetches only while a window shows BUFFER (a buried tail makes no
requests); reschedules itself unless the run is terminal."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (cond
       (agmon--tail-terminal nil)
       ((not (get-buffer-window buffer 'visible))
        (agmon--tail-schedule buffer))   ; keep the schedule alive, skip the fetch
       (t
        (agmon--events
         agmon--tail-run-id agmon--tail-cursor
         (lambda (batch)
           (when (buffer-live-p buffer)
             (with-current-buffer buffer
               (agmon--tail-consume batch)
               (unless agmon--tail-terminal (agmon--tail-schedule buffer)))))
         (lambda (err)
           (agmon--report-request-failure "tail" err)
           (unless (and (buffer-live-p buffer)
                        (with-current-buffer buffer agmon--tail-terminal))
             (agmon--tail-schedule buffer)))))))))

(defun agmon--tail-consume (batch)
  "Render BATCH's events into the buffer and act on terminal signals."
  (let ((events (alist-get 'events batch))
        (next (alist-get 'next_after batch)))
    (agmon--tail-append-events events agmon--tail-show-all)
    (when next (setq agmon--tail-cursor next))
    (cond
     ;; A result event means the run finished/errored: stop now, verdict async.
     ((seq-find (lambda (e) (equal (agmon--event-type e) "result")) events)
      (setq agmon--tail-terminal t)
      (agmon--tail-cancel-timer)
      (agmon--tail-conclude))
     ;; An empty poll: the non-result terminal states (interrupted/died) and
     ;; the stall heartbeat can only be told apart via /summary.
     ((null events)
      (agmon--tail-check-status)))))

(defun agmon--tail-conclude ()
  "Fetch the summary and print the terminal verdict line for this tail."
  (let ((buffer (current-buffer)))
    (agmon--summary
     agmon--tail-run-id
     (lambda (summary)
       (when (buffer-live-p buffer)
         (with-current-buffer buffer (agmon--tail-finish summary)))))))

(defun agmon--tail-check-status ()
  "On an empty poll, fetch the summary and conclude if terminal, else heartbeat."
  (let ((buffer (current-buffer)))
    (agmon--summary
     agmon--tail-run-id
     (lambda (summary)
       (when (buffer-live-p buffer)
         (with-current-buffer buffer
           (let* ((status (alist-get 'status summary))
                  (eff (alist-get 'effective_status status)))
             (cond
              ((member eff agmon--tail-terminal-statuses)
               (agmon--tail-finish summary))
              ((and (equal eff "stalled") (not agmon--tail-stalled-shown))
               (setq agmon--tail-stalled-shown t)
               (agmon--tail-insert
                (list (agmon--tail-heartbeat
                       (alist-get 'stalled_seconds status)))))))))))))

(defun agmon--tail-finish (summary)
  "Print the verdict from SUMMARY, stop polling, and message the outcome."
  (setq agmon--tail-terminal t)
  (agmon--tail-cancel-timer)
  (let* ((status (alist-get 'status summary))
         (metrics (alist-get 'metrics summary))
         (eff (or (alist-get 'effective_status status) "?")))
    (agmon--tail-insert (list (agmon--tail-summary-line status metrics)))
    (message "agmon: tail %s %s" (agmon--short-id agmon--tail-run-id) eff)))

(defun agmon--tail-revert (&rest _)
  "Restart this tail from the first event, re-rendering with the current filter.
Installed as `revert-buffer-function', so `g' (or `gr' under evil) restarts."
  (setq agmon--tail-cursor 0
        agmon--tail-terminal nil
        agmon--tail-stalled-shown nil)
  (let ((inhibit-read-only t))
    (remove-overlays)                     ; drop the old prose folds
    (erase-buffer))
  (agmon--tail-poll (current-buffer)))

(defun agmon-tail-toggle-line ()
  "Fold the assistant prose at point open or closed, flipping its marker.
No-op with a message when point is not on a foldable prose line."
  (interactive)
  (let ((ov (get-text-property (point) 'agmon-prose-ov)))
    (if (not ov)
        (message "agmon: no foldable prose here")
      (let ((now-collapsed (not (overlay-get ov 'invisible)))
            (mark (overlay-get ov 'agmon-mark)))
        (overlay-put ov 'invisible (and now-collapsed 'agmon-prose))
        (when mark
          (overlay-put mark 'before-string
                       (agmon--tail-fold-marker now-collapsed)))))))

(defun agmon-tail-toggle-all ()
  "Toggle showing every event type, including the low-value ones, then restart."
  (interactive)
  (setq agmon--tail-show-all (not agmon--tail-show-all))
  (message "agmon: tail showing %s events"
           (if agmon--tail-show-all "all" "curated"))
  (agmon--tail-revert))

(defun agmon-tail (&optional run-id)
  "Open a live event tail for RUN-ID, defaulting to the run at point.
Works from the run list and a detail buffer.  The buffer polls while
visible and stops at a terminal status; `a' toggles the low-value
filter, `TAB' folds assistant prose open/closed, `q' buries, `g'
restarts."
  (interactive)
  (let ((id (or run-id (agmon--run-id-at-point))))
    (unless id (user-error "No run at point"))
    (let ((buf (get-buffer-create (format "*agmon-tail: %s*" (agmon--short-id id)))))
      (with-current-buffer buf
        (agmon-tail-mode)
        (setq agmon--tail-run-id id
              agmon--tail-cursor 0
              agmon--tail-terminal nil
              agmon--tail-stalled-shown nil)
        (let ((inhibit-read-only t)) (remove-overlays) (erase-buffer)))
      (pop-to-buffer buf)
      (agmon--tail-poll buf))))

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

(defun agmon--columns-with-phase (columns)
  "Return COLUMNS with `phase' added (after `status' if present, else appended)."
  (cond
   ((memq 'phase columns) columns)
   ((memq 'status columns)
    (mapcan (lambda (c) (if (eq c 'status) (list 'status 'phase) (list c)))
            columns))
   (t (append columns '(phase)))))

(defun agmon--read-pipeline ()
  "Read a pipeline id, completing over the pipelines present in the fleet."
  (let ((pipelines (delete-dups
                    (delq nil (mapcar (lambda (r)
                                        (alist-get 'pipeline (alist-get 'labels r)))
                                      (agmon--runs))))))
    (completing-read "Pipeline: " pipelines nil nil)))

;;;###autoload
(defun agmon-list-pipeline (pipeline)
  "Open a run list filtered to PIPELINE (the reserved `pipeline' label).
The list adds a Phase column, shows the filter in its header line, and
reuses one buffer per pipeline.  Interactively, completes over the
pipelines currently in the fleet."
  (interactive (list (agmon--read-pipeline)))
  (let ((buf (get-buffer-create (format "*agmon: pipeline=%s*" pipeline))))
    (with-current-buffer buf
      (agmon-list-mode)
      (setq-local agmon-list-columns (agmon--columns-with-phase agmon-list-columns))
      (setq agmon--list-labels (list (format "pipeline=%s" pipeline)))
      (setq header-line-format
            (list (propertize " Pipeline: " 'face 'shadow)
                  (propertize pipeline 'face 'agmon-detail-heading)))
      ;; Rebuild the header now that the columns include Phase.
      (setq tabulated-list-format (agmon--list-format))
      (tabulated-list-init-header)
      (agmon--list-refresh)
      (tabulated-list-print))
    (pop-to-buffer buf)))

;;;; Cost rollup
;;
;; A fleet-wide view (not per-run): GET /v1/stats/costs, one row per day
;; plus a pinned totals row, rendered as a second `tabulated-list-mode'
;; screen.  It shares the run list's numeric-sort machinery but keeps its
;; own tiny mode; unlike the list it does not auto-refresh (costs move
;; slowly -- `g' re-fetches by hand).

(defun agmon--costs-refresh ()
  "Re-fetch the cost rollup into `tabulated-list-entries', synchronously.
Installed on `tabulated-list-revert-hook', so \\[revert-buffer] refreshes."
  (setq tabulated-list-entries
        (agmon--cost-entries (agmon--request "/v1/stats/costs"))))

(define-derived-mode agmon-costs-mode tabulated-list-mode "Agmon-Costs"
  "Major mode for the agmon cost rollup: a daily cost/turns/run-count table.

One row per day, newest first, with a pinned TOTAL row; column headers
sort, o restores the default sort, g re-fetches, q buries, and ? shows
this help.

\\{agmon-costs-mode-map}"
  (setq tabulated-list-format (agmon--costs-format))
  ;; Newest day first; the `t' flips the numeric Date sort to descending.
  (setq tabulated-list-sort-key (cons "Date" t))
  (setq tabulated-list-padding 1)
  (add-hook 'tabulated-list-revert-hook #'agmon--costs-refresh nil t)
  (tabulated-list-init-header))

(defun agmon-costs-sort-default ()
  "Restore the default newest-day-first sort in the cost buffer."
  (interactive)
  (setq tabulated-list-sort-key (cons "Date" t))
  (tabulated-list-init-header)
  (tabulated-list-print t))

;; `o' restores the default sort after a column-header click, mirroring the
;; run list.  (`g' revert and `q' bury come from `tabulated-list-mode'.)
(keymap-set agmon-costs-mode-map "o" #'agmon-costs-sort-default)
(keymap-set agmon-costs-mode-map "?" #'describe-mode)

;;;###autoload
(defun agmon-costs ()
  "Open the *agmon-costs* buffer: the fleet's daily cost rollup."
  (interactive)
  (let ((buf (get-buffer-create "*agmon-costs*")))
    (with-current-buffer buf
      (agmon-costs-mode)
      (agmon--costs-refresh)
      (tabulated-list-print))
    (pop-to-buffer buf)))

;;;; Development helpers

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
