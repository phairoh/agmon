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

(defcustom agmon-detail-table-cell-width 36
  "Maximum rendered width of a Markdown table cell in the detail buffer.
Cells longer than this are truncated with an ellipsis, so a wide table
stays aligned and narrow enough to read without soft-wrapping.  Raise it
for wide frames, lower it for narrow ones."
  :type 'integer
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

(defun agmon--request (path &optional raw)
  "Perform a synchronous GET of PATH and return the JSON body.
PATH is a route beginning with a slash, e.g. \"/v1/runs\"; it is
appended to `agmon-url'.  By default the body is parsed per the JSON
representation above; with RAW non-nil it is returned verbatim as a
string (for the raw-JSON escape hatch).  Signals a `plz-error' if the
request fails."
  (let ((url (concat (string-remove-suffix "/" agmon-url) path)))
    (plz 'get url
      :as (if raw
              'string
            (lambda ()
              (json-parse-buffer :object-type 'alist
                                 :array-type 'list
                                 :null-object nil
                                 :false-object nil))))))

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

(defun agmon--summary (run-id)
  "Fetch the parsed /summary payload for RUN-ID.
The reply is a nested alist: `run' (the full record), `status',
`activity', `issues', `metrics', and `result_text'."
  (agmon--request (format "/v1/runs/%s/summary" run-id)))

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

(defun agmon--render-summary (summary now show-issues)
  "Render SUMMARY, a parsed /summary payload, to a display string as of NOW.
SHOW-ISSUES non-nil expands the per-issue detail; otherwise only the
Issues heading shows.  Pure: it takes data and returns a propertized
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
           (dur-str (string-join
                     (delq nil
                           (list (and dur (agmon--format-age dur))
                                 (and (numberp .run.exit_code)
                                      (format "exit %d" .run.exit_code))))
                     "   ·   "))
           (cost-str (string-join
                      (delq nil
                            (list (agmon--nonempty
                                   (agmon--format-cost .run.total_cost_usd))
                                  (and .run.num_turns
                                       (format "%d turns" .run.num_turns))
                                  (and .run.event_count
                                       (format "%d events" .run.event_count))))
                      "   ·   "))
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
      (dolist (field (delq nil
                           (list (agmon--detail-field "Path" .run.cwd)
                                 (agmon--detail-field "Git" git)
                                 (agmon--detail-field "Host" .run.host)
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
      ;; Issues, collapsed to the heading unless SHOW-ISSUES (they are
      ;; usually the routine \"read before edit\" kind, so hide by default).
      (when .issues
        (push "" lines)
        (push (concat (propertize (format "Issues (%d)" (length .issues))
                                  'face 'agmon-detail-heading)
                      "   "
                      (propertize (if show-issues "TAB to hide" "TAB to show")
                                  'face 'shadow))
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

;; `RET' drills into the run at point.  (evil users: this key is only
;; reachable because agmon-list-mode-map is marked as an overriding map
;; in the operator's config; see the package README.)
(keymap-set agmon-list-mode-map "RET" #'agmon-show-run)

;; `J' shows the raw /summary JSON for the run at point.
(keymap-set agmon-list-mode-map "J" #'agmon-show-json)

(defun agmon--list-refresh ()
  "Re-fetch the run list into `tabulated-list-entries'.
Installed on `tabulated-list-revert-hook', so `g' refreshes."
  (setq tabulated-list-entries (agmon--run-entries (agmon--runs))))

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
`g' refreshes it from the server; the `TAB' issues toggle re-renders
from this cache without a network round-trip.")

(defvar-local agmon--detail-show-issues nil
  "Non-nil when this buffer expands the per-issue detail.
Buffer-local and off by default -- issues are usually routine.  `TAB'
flips it.")

;; `q' (bury) and `g' (revert) are inherited from `special-mode'; we add
;; TAB to expand/collapse the issues section.  Bind both spellings: `TAB'
;; is the terminal form (C-i), `<tab>' the event a graphical frame's Tab
;; key actually sends -- binding only one leaves the other dead on GUIs.
(defvar-keymap agmon-detail-mode-map
  :doc "Keymap for `agmon-detail-mode'."
  "TAB" #'agmon-detail-toggle-issues
  "<tab>" #'agmon-detail-toggle-issues
  "J" #'agmon-show-json)

(define-derived-mode agmon-detail-mode special-mode "Agmon-Detail"
  "Major mode for a single run's detail view.

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
  "Fetch this buffer's run summary into the cache, then redraw."
  (setq agmon--detail-summary (agmon--summary agmon--detail-run-id))
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
                                   agmon--detail-show-issues))
    (goto-char (min p (point-max)))))

(defun agmon-detail-toggle-issues ()
  "Expand or collapse the per-issue detail in this run's detail buffer."
  (interactive)
  (setq agmon--detail-show-issues (not agmon--detail-show-issues))
  (agmon--detail-render))

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
;; when the rendered view hides something you need to see.  It derives
;; from `js-json-mode' for syntax highlighting (regexp-based, so no
;; tree-sitter grammar required) and adds a read-only, `q'-to-bury view;
;; unlike a `special-mode' buffer that means evil users bind `q'/`g'
;; explicitly (see the README).

(defvar-local agmon--json-run-id nil
  "Run id whose raw /summary this JSON buffer shows.
Buffer-local, so `g' knows what to re-fetch.")

(defvar-keymap agmon-json-mode-map
  :doc "Keymap for `agmon-json-mode'."
  "q" #'quit-window
  "g" #'revert-buffer)

(define-derived-mode agmon-json-mode js-json-mode "Agmon-JSON"
  "Major mode for agmon's raw-JSON escape hatch.
A read-only, syntax-highlighted view of a run's /summary payload; `q'
buries it and `g' re-fetches."
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
