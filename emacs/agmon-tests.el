;;; agmon-tests.el --- Tests for agmon.el  -*- lexical-binding: t; -*-

;; Copyright (C) 2026  Aaron Trachtman

;; Author: Aaron Trachtman <phairoh@gmail.com>
;; Keywords: tools, processes

;; This file is not part of GNU Emacs.

;;; Commentary:

;; ERT tests for agmon's pure layer -- the functions that take plain data
;; (alists parsed from the HTTP JSON) and return strings, faces, or
;; `tabulated-list' entries, with no network or buffer state.  That is the
;; layer worth testing in isolation; the transport and the buffers are
;; thin wrappers around it.
;;
;; Run them interactively in a running Emacs (where `plz' is loaded):
;;
;;     M-x ert RET t RET
;;
;; or headless in batch, with `plz' reachable on the load-path:
;;
;;     emacs -batch -L /path/to/plz -L . \
;;       -l ert -l agmon-tests.el -f ert-run-tests-batch-and-exit
;;
;; (agmon requires plz at load time; the tests here never call it.)

;;; Code:

(require 'ert)
(require 'agmon)

;;;; Time and number formatting

(ert-deftest agmon-test-format-age ()
  "`agmon--format-age' renders seconds as a compact human age."
  (should (equal (agmon--format-age nil) ""))
  (should (equal (agmon--format-age 0) "0s"))
  (should (equal (agmon--format-age 45) "45s"))
  (should (equal (agmon--format-age 60) "1m"))
  (should (equal (agmon--format-age 599) "9m"))
  (should (equal (agmon--format-age 3600) "1h"))
  (should (equal (agmon--format-age 3661) "1h1m"))
  (should (equal (agmon--format-age 86400) "1d"))
  (should (equal (agmon--format-age 180000) "2d")))

(ert-deftest agmon-test-format-cost ()
  "`agmon--format-cost' renders a number as dollars, else the empty string."
  (should (equal (agmon--format-cost 1.2345) "$1.23"))
  (should (equal (agmon--format-cost 0) "$0.00"))
  (should (equal (agmon--format-cost nil) ""))
  (should (equal (agmon--format-cost "nope") "")))

;;;; String helpers

(ert-deftest agmon-test-short-id ()
  "`agmon--short-id' returns the hex tail after the final dash."
  (should (equal (agmon--short-id "20260710T012416-efb89a") "efb89a"))
  (should (equal (agmon--short-id "no-dash-hex-here") "no-dash-hex-here"))
  (should (equal (agmon--short-id "plain") "plain")))

(ert-deftest agmon-test-abbrev-path ()
  "`agmon--abbrev-path' keeps only the final path component."
  (should (equal (agmon--abbrev-path "/home/aaron/src/agmon") "agmon"))
  (should (equal (agmon--abbrev-path "src/agmon") "agmon"))
  (should (equal (agmon--abbrev-path "/only") "only"))
  (should (equal (agmon--abbrev-path "") "")))

(ert-deftest agmon-test-oneline ()
  "`agmon--oneline' collapses whitespace runs and trims."
  (should (equal (agmon--oneline "  a  b\n c ") "a b c"))
  (should (equal (agmon--oneline "") ""))
  (should (equal (agmon--oneline nil) "")))

(ert-deftest agmon-test-truncate ()
  "`agmon--truncate' cuts to WIDTH with an ellipsis, else leaves it."
  (should (equal (agmon--truncate "hello" 3) "he…"))
  (should (equal (agmon--truncate "hi" 10) "hi"))
  (should (equal (agmon--truncate "abc" nil) "abc")))

(ert-deftest agmon-test-nonempty ()
  "`agmon--nonempty' returns non-empty strings, else nil."
  (should (equal (agmon--nonempty "x") "x"))
  (should-not (agmon--nonempty ""))
  (should-not (agmon--nonempty nil)))

;;;; Labels

(ert-deftest agmon-test-labels-cell ()
  "`agmon--labels-cell' renders sorted k=v pairs, empty for none."
  (should (equal (agmon--labels-cell '((pipeline . "build") (phase . "test")))
                 "phase=test,pipeline=build"))
  (should (equal (agmon--labels-cell nil) "")))

;;;; Run-list entry construction

(defconst agmon-test--run
  '((run_id . "20260710T012416-efb89a")
    (effective_status . "running")
    (cwd . "/home/aaron/src/agmon")
    (started_at . "2026-07-10T01:24:16Z")
    (total_cost_usd . 1.5)
    (labels . ((pipeline . "build") (phase . "test")))
    (prompt_preview . "do the thing"))
  "A canned run alist shaped like the parsed /v1/runs JSON.")

(ert-deftest agmon-test-run-cell ()
  "`agmon--run-cell' renders each column from a run alist."
  ;; With no label promoted to its own column, `labels' shows them all.
  (let ((now (current-time))
        (agmon-list-columns '(id status cwd age cost labels task)))
    (should (equal (agmon--run-cell 'id agmon-test--run now) "efb89a"))
    (should (equal (agmon--run-cell 'status agmon-test--run now) "running"))
    (should (equal (agmon--run-cell 'cwd agmon-test--run now) "agmon"))
    (should (equal (agmon--run-cell 'phase agmon-test--run now) "test"))
    (should (equal (agmon--run-cell 'labels agmon-test--run now)
                   "phase=test,pipeline=build"))
    (should (equal (agmon--run-cell 'task agmon-test--run now) "do the thing"))
    (let ((cost (agmon--run-cell 'cost agmon-test--run now)))
      (should (equal cost "$1.50"))
      ;; Numeric columns stash their raw value for value-based sorting.
      (should (= (get-text-property 0 'agmon-sort cost) 1.5)))))

(defconst agmon-test--labelled-run
  '((run_id . "20260713T172231-2c9e67")
    (model . "opus")
    (labels . ((pipeline . "artifacts-006")
               (phase . "build")
               (parent . "20260709T090000-abc123")
               (experiment . "ab"))))
  "A run carrying a model field and the reserved lineage labels.")

(ert-deftest agmon-test-run-cell-label-columns ()
  "Label-backed columns read their label; `model' is a field; parent short-ids."
  (should (equal (agmon--run-cell 'model agmon-test--labelled-run nil) "opus"))
  (should (equal (agmon--run-cell 'pipeline agmon-test--labelled-run nil)
                 "artifacts-006"))
  (should (equal (agmon--run-cell 'phase agmon-test--labelled-run nil) "build"))
  ;; `parent' is a run id, rendered as its memorable tail.
  (should (equal (agmon--run-cell 'parent agmon-test--labelled-run nil) "abc123"))
  ;; A null/absent field or label renders as the empty string, never nil.
  (should (equal (agmon--run-cell 'model '((run_id . "x")) nil) ""))
  (should (equal (agmon--run-cell 'parent '((run_id . "x")) nil) "")))

(ert-deftest agmon-test-labels-column-excludes-promoted ()
  "The `labels' column omits any label promoted to its own column."
  (let ((labels (alist-get 'labels agmon-test--labelled-run)))
    ;; No label column shown: every label survives.
    (let ((agmon-list-columns '(id labels)))
      (should-not (agmon--promoted-label-keys))
      (should (equal (agmon--labels-cell (agmon--unpromoted-labels labels))
                     "experiment=ab,parent=20260709T090000-abc123,\
phase=build,pipeline=artifacts-006")))
    ;; Promote pipeline, phase, parent: only the leftover label remains.
    (let ((agmon-list-columns '(id pipeline phase parent labels)))
      (should (equal (agmon--promoted-label-keys) '(pipeline phase parent)))
      (should (equal (agmon--unpromoted-labels labels) '((experiment . "ab"))))
      ;; End to end through the cell: the leftover fits, so no truncation.
      (should (equal (agmon--run-cell 'labels agmon-test--labelled-run nil)
                     "experiment=ab")))))

(ert-deftest agmon-test-run-duration-seconds ()
  "`agmon--run-duration-seconds' spans start->end, or start->now while live."
  ;; Finished: ended_at pins the run time regardless of NOW.
  (let ((run '((started_at . "2026-07-10T01:00:00Z")
               (ended_at . "2026-07-10T01:15:30Z")))
        (now (agmon--parse-time "2026-07-11T00:00:00Z")))
    (should (= (agmon--run-duration-seconds run now) 930)))
  ;; Live: no ended_at, so it ticks against NOW.
  (let ((run '((started_at . "2026-07-10T01:00:00Z")))
        (now (agmon--parse-time "2026-07-10T01:02:00Z")))
    (should (= (agmon--run-duration-seconds run now) 120)))
  ;; Unknown start -> nil, not an error.
  (should-not (agmon--run-duration-seconds '((cwd . "/x")) (current-time))))

(ert-deftest agmon-test-run-cell-runtime ()
  "The `runtime' cell renders the duration and stashes it for sorting."
  (let* ((run '((started_at . "2026-07-10T01:00:00Z")
                (ended_at . "2026-07-10T03:13:00Z")))
         (now (agmon--parse-time "2026-07-11T00:00:00Z"))
         (cell (agmon--run-cell 'runtime run now)))
    (should (equal (substring-no-properties cell) "2h13m"))
    (should (= (get-text-property 0 'agmon-sort cell) 7980))))

(ert-deftest agmon-test-run-cell-started ()
  "The `started' cell renders the local calendar date, empty when unknown."
  (let ((cell (agmon--run-cell 'started
                               '((started_at . "2026-07-10T01:24:16Z")) nil)))
    ;; Timezone-independent shape check: a bare YYYY-MM-DD date.
    (should (string-match-p "\\`[0-9]\\{4\\}-[0-9]\\{2\\}-[0-9]\\{2\\}\\'" cell)))
  (should (equal (agmon--run-cell 'started '((cwd . "/x")) nil) "")))

(ert-deftest agmon-test-run-cell-status-face ()
  "The status cell carries the per-status face."
  (let ((cell (agmon--run-cell 'status agmon-test--run (current-time))))
    (should (eq (get-text-property 0 'face cell) 'agmon-status-running))))

(ert-deftest agmon-test-run-entry-carries-full-id ()
  "`agmon--run-entry' keys the row on the full run id, not the short one."
  (let ((agmon-list-columns '(id status cost)))
    (let ((entry (agmon--run-entry agmon-test--run (current-time))))
      (should (equal (car entry) "20260710T012416-efb89a"))
      (should (= (length (cadr entry)) 3)))))

;;;; Session lineage split

(defconst agmon-test--session-runs
  '(((run_id . "A") (session_id . "S") (started_at . "2026-07-10T01:00:00Z"))
    ((run_id . "B") (session_id . "S") (started_at . "2026-07-10T02:00:00Z"))
    ((run_id . "C") (session_id . "S") (started_at . "2026-07-10T03:00:00Z"))
    ((run_id . "X") (session_id . "OTHER") (started_at . "2026-07-10T01:30:00Z")))
  "Runs across two sessions, for lineage tests.")

(defun agmon-test--ids (runs)
  "Return the run_id of each run in RUNS."
  (mapcar (lambda (r) (alist-get 'run_id r)) runs))

(ert-deftest agmon-test-lineage-splits-around-run ()
  "`agmon--lineage' splits same-session runs into before/after by start."
  (let ((lin (agmon--lineage "B" "S" agmon-test--session-runs)))
    (should (equal (agmon-test--ids (car lin)) '("A")))    ; resumed from
    (should (equal (agmon-test--ids (cdr lin)) '("C")))))  ; resumed by

(ert-deftest agmon-test-lineage-nil-session ()
  "`agmon--lineage' returns nil when the session id is nil."
  (should-not (agmon--lineage "B" nil agmon-test--session-runs)))

;;;; Cost rollup

(ert-deftest agmon-test-cost-epoch ()
  "`agmon--cost-epoch' orders dates chronologically, 0 for garbage."
  (should (> (agmon--cost-epoch "2026-07-09") (agmon--cost-epoch "2026-07-08")))
  (should (= (agmon--cost-epoch "not a date") 0)))

(defconst agmon-test--costs
  '((buckets . (((bucket . "2026-07-08") (runs . 5) (total_cost_usd . 5.28) (total_turns . 77))
                ((bucket . "2026-07-09") (runs . 7) (total_cost_usd . 19.15) (total_turns . 249))))
    (totals . ((runs . 12) (total_cost_usd . 24.43) (total_turns . 326))))
  "A canned /v1/stats/costs payload.")

(ert-deftest agmon-test-cost-entries-rows ()
  "`agmon--cost-entries' builds one row per bucket plus a totals row."
  (let ((entries (agmon--cost-entries agmon-test--costs)))
    (should (= (length entries) 3))
    (let ((first (cadr (nth 0 entries))))
      (should (equal (aref first 0) "2026-07-08"))
      (should (equal (aref first 1) "5"))
      (should (equal (aref first 2) "$5.28"))
      (should (equal (aref first 3) "77"))
      (should (= (get-text-property 0 'agmon-sort (aref first 1)) 5)))))

(ert-deftest agmon-test-cost-entries-totals-row ()
  "The final row sums the fleet, is bold, and sorts to an edge (sort 0)."
  (let* ((entries (agmon--cost-entries agmon-test--costs))
         (totals (car (last entries)))
         (vec (cadr totals)))
    (should (eq (car totals) 'agmon-totals))
    (should (equal (aref vec 0) "TOTAL"))
    (should (equal (aref vec 2) "$24.43"))
    (should (eq (get-text-property 0 'face (aref vec 0)) 'agmon-costs-total))
    (should (= (get-text-property 0 'agmon-sort (aref vec 2)) 0))))

;;;; Event summaries (the tail's editorial line)

(ert-deftest agmon-test-summarize-result-success ()
  "A successful result reads status, cost, and turns, with the success face."
  (let ((ev '((type . "result")
              (payload . ((type . "result") (subtype . "success")
                          (total_cost_usd . 2.5) (num_turns . 10))))))
    (should (equal (agmon--summarize-event ev)
                   (cons "result: success · $2.50 · 10 turns" 'success)))))

(ert-deftest agmon-test-summarize-result-error ()
  "A non-success result gets the error face and no cost when absent."
  (let ((ev '((type . "result")
              (payload . ((subtype . "error_max_turns") (num_turns . 5))))))
    (should (equal (agmon--summarize-event ev)
                   (cons "result: error_max_turns · 5 turns" 'error)))))

(ert-deftest agmon-test-summarize-assistant-tool-use ()
  "An assistant tool_use turn shows the arrow, tool name, and target."
  (let ((ev '((type . "assistant")
              (payload . ((message . ((content . (((type . "tool_use")
                                                   (name . "Read")
                                                   (input . ((file_path . "/x/y.el")))))))))))))
    (should (equal (agmon--summarize-event ev) (cons "→ Read /x/y.el" 'shadow)))))

(ert-deftest agmon-test-summarize-assistant-progress ()
  "An assistant PROGRESS line is surfaced with the progress face."
  (let ((ev '((type . "assistant")
              (payload . ((message . ((content . (((type . "text")
                                                   (text . "PROGRESS: building widget")))))))))))
    (should (equal (agmon--summarize-event ev)
                   (cons "PROGRESS: building widget" 'agmon-tail-progress)))))

(ert-deftest agmon-test-summarize-user-tool-result ()
  "A user tool_result summarizes its content snippet."
  (let ((ev '((type . "user")
              (payload . ((message . ((content . (((type . "tool_result")
                                                   (content . "file contents")))))))))))
    (should (equal (agmon--summarize-event ev)
                   (cons "tool_result: file contents" 'shadow)))))

(ert-deftest agmon-test-summarize-user-error ()
  "An errored tool_result reads as an error with its snippet."
  (let ((ev '((type . "user")
              (payload . ((message . ((content . (((type . "tool_result")
                                                   (is_error . t)
                                                   (content . "boom")))))))))))
    (should (equal (agmon--summarize-event ev) (cons "error: boom" 'error)))))

(ert-deftest agmon-test-summarize-system-and-unparseable ()
  "System events name their subtype; an unparseable line is flagged."
  (should (equal (agmon--summarize-event '((type . "system") (subtype . "init")))
                 (cons "system: init" 'shadow)))
  (should (equal (agmon--summarize-event '((type . "_unparseable")))
                 (cons "<unparseable line>" 'error))))

(ert-deftest agmon-test-event-progress-returns-last ()
  "`agmon--event-progress' returns the last PROGRESS line in a block."
  (let ((ev '((type . "assistant")
              (payload . ((message . ((content . (((type . "text")
                                                   (text . "PROGRESS: one\nmid\nPROGRESS: two")))))))))))
    (should (equal (agmon--event-progress ev) "two"))))

(provide 'agmon-tests)
;;; agmon-tests.el ends here
