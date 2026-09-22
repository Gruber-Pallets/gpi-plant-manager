from pathlib import Path
import subprocess


def test_identical_inflight_validation_is_shared_but_edits_and_later_checks_stay_fresh():
    source = Path('src/zira_dashboard/static/staffing.js').read_text()
    function = source.split('    async function validateCurrentView() {', 1)[1].split('    function invalidateCurrentViewValidation()', 1)[0]
    script = '''
    let __viewingPosted=false, validationRequestId=0, validationController=null;
    let validationInFlight=false, validationPayload=null;
    const window={SCHEDULE_PUBLISHED:false};
    let snapshot={day:'2026-09-21',assignments:{Repair:['Lee']}};
    function currentViewSnapshot(){return snapshot;}
    function renderCoverageIssues(){}
    function validationUnavailableIssue(){return {};}
    let pending=[],calls=0;
    function fetch(url,opts){calls++;return new Promise(resolve=>pending.push(()=>resolve({ok:true,json:async()=>({ok:true,issues:[]})})));}
    async function validateCurrentView(){
    ''' + function + '''
    (async()=>{
      const first=validateCurrentView();
      void validateCurrentView();
      if(calls!==1) throw new Error('identical in-flight validation was sent twice');
      snapshot={day:'2026-09-21',assignments:{Repair:['Sam']}};
      const changed=validateCurrentView();
      if(calls!==2) throw new Error('changed assignments were not validated');
      pending.shift()();await first;
      void validateCurrentView();
      if(calls!==2) throw new Error('stale response cleared newer in-flight marker');
      pending.shift()();await changed;
      const fresh=validateCurrentView();
      if(calls!==3) throw new Error('completed results incorrectly cached across later checks');
      pending.shift()();await fresh;
    })().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
