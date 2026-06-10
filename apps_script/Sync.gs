/**
 * Options Tracker — Sheet-side sync trigger.
 *
 * Adds a "⚡ Sync" menu to the spreadsheet. Picking "Sync now" writes a unique
 * token to the control cell (Sync!A1). The VM-side poller (sheet_trigger.py)
 * notices the new token, runs ./sync, and writes a status line to Sync!A2.
 *
 * This is a bound script — it lives inside the spreadsheet, not on the VM.
 *
 * --- One-time setup ---
 * 1. Open the Google Sheet → Extensions → Apps Script.
 * 2. Paste this file's contents into Code.gs (replace the default), Save.
 * 3. Reload the spreadsheet. The "⚡ Sync" menu appears after a moment.
 * 4. The first time you pick "Sync now", Google asks you to authorize the
 *    script (it only needs access to this spreadsheet). Approve once.
 *
 * The VM must be running sheet_trigger.py (systemd: options-sync-trigger) for
 * the request to actually do anything. Status shows up in Sync!A2 and in a
 * toast/alert.
 */

var CONTROL_TAB = 'Sync';
var REQUEST_CELL = 'A1';
var STATUS_CELL = 'A2';

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('⚡ Sync')
    .addItem('Sync now', 'requestSync')
    .addItem('Show last status', 'showStatus')
    .addToUi();
}

function _controlSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(CONTROL_TAB);
  if (!sh) {
    sh = ss.insertSheet(CONTROL_TAB);
    sh.getRange(REQUEST_CELL).setNote('Sync request token — written by the ⚡ Sync menu.');
    sh.getRange(STATUS_CELL).setNote('Last sync status — written by the VM poller.');
  }
  return sh;
}

function requestSync() {
  var sh = _controlSheet();
  // A unique, monotonic token so the poller always sees a fresh request,
  // even if the cell already held a previous one.
  var token = 'REQ ' + Utilities.formatDate(
    new Date(), 'America/Los_Angeles', "yyyy-MM-dd'T'HH:mm:ss");
  sh.getRange(REQUEST_CELL).setValue(token);
  sh.getRange(STATUS_CELL).setValue('⏳ Requested ' + token + ' — waiting for VM…');
  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Sync requested. The VM picks it up within ~30s; watch ' +
    CONTROL_TAB + '!' + STATUS_CELL + '.', 'Sync requested', 5);
}

function showStatus() {
  var sh = _controlSheet();
  var status = sh.getRange(STATUS_CELL).getValue() || '(no status yet)';
  SpreadsheetApp.getUi().alert('Last sync status', String(status),
    SpreadsheetApp.getUi().ButtonSet.OK);
}
