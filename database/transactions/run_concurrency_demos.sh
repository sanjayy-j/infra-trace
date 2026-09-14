#!/bin/sh
# =====================================================================
# InfraTrace - automated concurrency demonstration
# File   : database/transactions/run_concurrency_demos.sh
#
# The demos in 02..05 are written for two terminals driven by hand, which
# is the clearest way to SEE what happens. This script drives the same
# scenarios automatically with two real concurrent connections, so the
# results are reproducible and can be captured for a report.
#
# It uses background subshells plus DO SLEEP() to force the exact
# interleaving each demo needs. Nothing here is simulated: every block
# below is a separate client connection to the same server.
#
# Usage
#
# The MYSQL variable is REQUIRED - this script stores no credentials and has no
# default. Supply a client invocation that can already authenticate.
#
#   Local MySQL (recommended: a login-path, so no password is typed at all):
#       mysql_config_editor set --login-path=infratrace --user=root --password
#       MYSQL="mysql --login-path=infratrace" sh transactions/run_concurrency_demos.sh
#
#   Local MySQL, prompting for the password instead:
#       MYSQL="mysql -u root -p" sh transactions/run_concurrency_demos.sh
#       (note: this prompts once per connection, and the demos open several)
#
#   Docker:
#       docker cp transactions/run_concurrency_demos.sh <container>:/tmp/
#       docker exec -e MYSQL="mysql -u root -pYOURPASS" <container> sh /tmp/run_concurrency_demos.sh
#
# Prerequisite: transactions/00_demo_setup.sql must have been run.
# =====================================================================

: "${MYSQL:?Set MYSQL first, e.g.  MYSQL='mysql -u root -p'  (no password is stored in this repository)}"
T="$MYSQL --table infratrace"     # table-formatted output
Q="$MYSQL -N -B infratrace"       # bare output, for scripting

reset_component() {
  $Q -e "UPDATE component SET criticality='High' WHERE component_id IN (9001,9002);" 2>/dev/null
}
reset_incident() {
  $Q -e "UPDATE incident SET root_cause=NULL WHERE incident_id=9001;" 2>/dev/null
}

# Guard: refuse to run if the demo rows are missing, rather than silently
# doing nothing or (worse) touching real rows.
if [ "$($Q -e 'SELECT COUNT(*) FROM component WHERE component_id=9001;' 2>/dev/null)" != "1" ]; then
  echo "ERROR: demo rows not found. Run transactions/00_demo_setup.sql first." >&2
  exit 1
fi

echo "#########################################################"
echo "# 1. ISOLATION: READ COMMITTED allows a non-repeatable read"
echo "#    expect: High, then Critical"
echo "#########################################################"
reset_component
( $T <<'EOF' 2>/dev/null
SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED;
START TRANSACTION;
SELECT criticality AS a_first_read FROM component WHERE component_id=9001;
DO SLEEP(3);
SELECT criticality AS a_second_read FROM component WHERE component_id=9001;
COMMIT;
EOF
) &
sleep 1
$Q -e "UPDATE component SET criticality='Critical' WHERE component_id=9001;" 2>/dev/null
echo "   [B committed criticality='Critical' while A's transaction was open]"
wait

echo ""
echo "#########################################################"
echo "# 2. ISOLATION: REPEATABLE READ holds a stable snapshot"
echo "#    expect: High, then High, then Critical after COMMIT"
echo "#########################################################"
reset_component
( $T <<'EOF' 2>/dev/null
SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION;
SELECT criticality AS a_first_read FROM component WHERE component_id=9001;
DO SLEEP(3);
SELECT criticality AS a_second_read FROM component WHERE component_id=9001;
COMMIT;
SELECT criticality AS a_after_commit FROM component WHERE component_id=9001;
EOF
) &
sleep 1
$Q -e "UPDATE component SET criticality='Critical' WHERE component_id=9001;" 2>/dev/null
echo "   [B committed criticality='Critical' while A's transaction was open]"
wait

echo ""
echo "#########################################################"
echo "# 3. ISOLATION: a DIRTY READ under READ UNCOMMITTED"
echo "#    expect: B sees 'Low', which is then rolled back"
echo "#########################################################"
reset_component
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
UPDATE component SET criticality='Low' WHERE component_id=9001;
DO SLEEP(3);
ROLLBACK;
EOF
) &
sleep 1
echo "   [A has UPDATEd to 'Low' but has NOT committed]"
$T -e "SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;
       SELECT criticality AS b_dirty_read FROM component WHERE component_id=9001;" 2>/dev/null
wait
echo "   [A ROLLED BACK - the value B saw never existed in committed state]"
$T -e "SELECT criticality AS b_read_after_rollback FROM component WHERE component_id=9001;" 2>/dev/null

echo ""
echo "#########################################################"
echo "# 4. LOST UPDATE: two responders, no locking"
echo "#    expect: only ONE finding survives"
echo "#########################################################"
reset_incident
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT CONCAT('   A reads: ', IFNULL(root_cause,'NULL')) FROM incident WHERE incident_id=9001;
DO SLEEP(2);
UPDATE incident SET root_cause='Connection pool exhausted under load.' WHERE incident_id=9001;
COMMIT;
EOF
) &
sleep 1
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT CONCAT('   B reads: ', IFNULL(root_cause,'NULL')) FROM incident WHERE incident_id=9001;
DO SLEEP(3);
UPDATE incident SET root_cause='Replica disk I/O saturated.' WHERE incident_id=9001;
COMMIT;
EOF
) &
wait
$T -e "SELECT root_cause AS final_value FROM incident WHERE incident_id=9001;" 2>/dev/null

echo ""
echo "#########################################################"
echo "# 5. LOST UPDATE PREVENTED: SELECT ... FOR UPDATE"
echo "#    expect: BOTH findings survive"
echo "#########################################################"
reset_incident
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT CONCAT('   A reads (FOR UPDATE): ', IFNULL(root_cause,'NULL')) FROM incident WHERE incident_id=9001 FOR UPDATE;
DO SLEEP(3);
UPDATE incident SET root_cause='Connection pool exhausted under load.' WHERE incident_id=9001;
COMMIT;
EOF
) &
sleep 1
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT CONCAT('   B reads (FOR UPDATE, after waiting for A): ', IFNULL(root_cause,'NULL')) FROM incident WHERE incident_id=9001 FOR UPDATE;
UPDATE incident SET root_cause=CONCAT(root_cause,' Also: replica disk I/O saturated.') WHERE incident_id=9001;
COMMIT;
EOF
) &
wait
$T -e "SELECT root_cause AS final_value FROM incident WHERE incident_id=9001;" 2>/dev/null

echo ""
echo "#########################################################"
echo "# 6. LOCK WAIT TIMEOUT  (expect ERROR 1205)"
echo "#########################################################"
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT component_id FROM component WHERE component_id=9001 FOR UPDATE;
DO SLEEP(6);
COMMIT;
EOF
) &
sleep 1
$MYSQL infratrace -e "SET SESSION innodb_lock_wait_timeout=3;
START TRANSACTION;
SELECT component_id FROM component WHERE component_id=9001 FOR UPDATE;" 2>&1 | grep -v "Using a password"
wait

echo ""
echo "#########################################################"
echo "# 7. NOWAIT, SKIP LOCKED, and the unblocked plain SELECT"
echo "#########################################################"
( $Q <<'EOF' 2>/dev/null
START TRANSACTION;
SELECT component_id FROM component WHERE component_id=9001 FOR UPDATE;
DO SLEEP(5);
COMMIT;
EOF
) &
sleep 1
echo "--- FOR UPDATE NOWAIT (expect ERROR 3572) ---"
$MYSQL infratrace -e "SELECT component_id FROM component WHERE component_id=9001 FOR UPDATE NOWAIT;" 2>&1 | grep -v "Using a password"
echo "--- FOR UPDATE SKIP LOCKED (expect only 9002) ---"
$T -e "SELECT component_id, component_name FROM component WHERE component_id IN (9001,9002) FOR UPDATE SKIP LOCKED;" 2>/dev/null
echo "--- plain SELECT: never blocked, reads the MVCC snapshot ---"
$T -e "SELECT criticality AS unblocked_read FROM component WHERE component_id=9001;" 2>/dev/null
echo "--- and the whole blast-radius analysis still runs ---"
$T -e "SELECT fn_blast_radius_count(9002) AS blast_radius_of_demo_db;" 2>/dev/null
wait

echo ""
echo "#########################################################"
echo "# 8. DEADLOCK: opposite lock ordering  (expect ERROR 1213)"
echo "#########################################################"
reset_component
DL_BEFORE=$($Q -e "SELECT count FROM information_schema.innodb_metrics WHERE name='lock_deadlocks';" 2>/dev/null)
( $MYSQL infratrace 2>&1 <<'EOF' | grep -v "Using a password" | sed 's/^/   [A] /'
START TRANSACTION;
UPDATE component SET criticality='Critical' WHERE component_id=9001;
DO SLEEP(2);
UPDATE component SET criticality='Low' WHERE component_id=9002;
COMMIT;
EOF
) &
( sleep 1
  $MYSQL infratrace 2>&1 <<'EOF' | grep -v "Using a password" | sed 's/^/   [B] /'
START TRANSACTION;
UPDATE component SET criticality='Critical' WHERE component_id=9002;
DO SLEEP(2);
UPDATE component SET criticality='Low' WHERE component_id=9001;
COMMIT;
EOF
) &
wait
DL_AFTER=$($Q -e "SELECT count FROM information_schema.innodb_metrics WHERE name='lock_deadlocks';" 2>/dev/null)
echo "   lock_deadlocks counter: $DL_BEFORE -> $DL_AFTER"
echo ""
echo "--- InnoDB post-mortem (excerpt of LATEST DETECTED DEADLOCK) ---"
$Q -e "SHOW ENGINE INNODB STATUS\G" 2>/dev/null \
  | sed -n '/LATEST DETECTED DEADLOCK/,/WE ROLL BACK/p' \
  | grep -vE '^ [0-9]+: len|^Record lock, heap' \
  | head -24

echo ""
echo "#########################################################"
echo "# 9. NO DEADLOCK when both sessions use a consistent order"
echo "#########################################################"
reset_component
( $MYSQL infratrace 2>&1 <<'EOF' | grep -v "Using a password" | sed 's/^/   [A] /'
START TRANSACTION;
UPDATE component SET criticality='Critical' WHERE component_id=9001;
DO SLEEP(2);
UPDATE component SET criticality='Low' WHERE component_id=9002;
COMMIT;
SELECT 'committed, no deadlock' AS outcome;
EOF
) &
( sleep 1
  $MYSQL infratrace 2>&1 <<'EOF' | grep -v "Using a password" | sed 's/^/   [B] /'
START TRANSACTION;
UPDATE component SET criticality='Critical' WHERE component_id=9001;
UPDATE component SET criticality='Low' WHERE component_id=9002;
COMMIT;
SELECT 'committed, no deadlock' AS outcome;
EOF
) &
wait
echo "   Both committed: B waited for A instead of deadlocking."

reset_component
reset_incident
echo ""
echo "All concurrency demonstrations complete. Demo rows left in place;"
echo "run transactions/99_demo_teardown.sql to remove them."
