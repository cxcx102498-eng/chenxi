// Observable renderer contracts: metadata stays offscreen; image motion settles;
// annotations remain attached and seeking is deterministic. No browser required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'engine/clips/y4_media_board.js'),'utf8');
function setup(data={}, imageData={}, extra=[]) {
  const sandbox={CLIPS:{},TY:{fit:(g,text,w,size)=>({ok:true,lines:[text],size})},
    CLIP:{fit:(iw,ih,x,y,w,h)=>{const s=Math.min(w/iw,h/ih);return{x:x+(w-iw*s)/2,y:y+(h-ih*s)/2,w:iw*s,h:ih*s,s};}}};
  vm.runInNewContext(source,sandbox);
  const image={width:800,height:450};
  const cues=[{kind:'image',at:0,image:'photo',asset_id:'A001',text:'Hidden Creator / License',data:{box:[0,0,1,1],label:'Person name',...imageData}},...extra];
  const ctx={box:{x:80,y:60,w:1760,h:840},W:1920,H:1080,u:1,safe:{top:60,bottom:180,left:80,right:80},FPS:30,dur:5,data,theme:{},IMG:{photo:image},of:(...kinds)=>cues.filter(q=>kinds.includes(q.kind))};
  const engine=sandbox.CLIPS.y4_media_board;engine.init(ctx);
  return t=>{
    const ops=[],stack=[];
    const g={globalAlpha:1,save(){stack.push(this.globalAlpha)},restore(){this.globalAlpha=stack.pop()},
      drawImage(im,...box){ops.push({kind:'image',box,alpha:this.globalAlpha})},
      fillText(text){ops.push({kind:'text',text})},
      ellipse(...values){ops.push({kind:'ellipse',values})}};
    for(const key of ['fillRect','beginPath','rect','clip','strokeRect','moveTo','lineTo','stroke'])g[key]=()=>{};
    engine.draw(g,t,ctx);return ops;
  };
}
const frame=setup();
assert(!frame(1).some(q=>q.text==='Person name'));
assert(setup({show_image_labels:true})(1).some(q=>q.text==='Person name'));
assert(!frame(1).some(q=>q.text?.includes('Hidden Creator')));
for(const t of [.15,1,2.2,4]) {
  const legacy=setup({}, {label:'Example Museum / 2025 / Photo credit',context_label:'Creator / CC BY'},[{kind:'focus',at:1,dur:.7,data:{asset_id:'A001',rect:[.2,.2,.3,.4]}}])(t);
  assert(!legacy.some(q=>q.kind==='text' && /Museum|2025|Creator|CC BY/.test(q.text)));
}
assert(!setup({show_image_labels:true})(1).some(q=>q.text?.includes('Hidden Creator')));
assert(setup({show_source_credits:true})(1).some(q=>q.text?.includes('Hidden Creator')));
const early=frame(.15)[0],settled=frame(1)[0];
assert(early.box[1]>settled.box[1] && early.alpha<1);
assert.deepEqual(settled,frame(4)[0]);
assert.deepEqual(setup({image_enter:'none'})(0)[0],setup({image_enter:'none'})(3)[0]);
const fade=setup({image_enter:'fade'});
assert.deepEqual(fade(.15)[0].box,fade(1)[0].box);
const zoom=setup({image_enter:'zoom-in'});
assert(zoom(.15)[0].box[2]<zoom(1)[0].box[2]);
const marked=setup({}, {}, [{kind:'highlight',at:.05,dur:.4,data:{asset_id:'A001',rect:[.2,.3,.4,.2],style:'circle'}}]);
for(const t of [.15,1]){
  const ops=marked(t),b=ops[0].box,e=ops.find(q=>q.kind==='ellipse').values;
  assert(Math.abs(e[0]-(b[0]+.4*b[2]))<1e-7);
  assert(Math.abs(e[1]-(b[1]+.4*b[3]))<1e-7);
}
const focused=setup({}, {}, [{kind:'focus',at:1,dur:.7,data:{asset_id:'A001',rect:[.2,.2,.3,.4]}},{kind:'reset',at:3,dur:.6,data:{asset_id:'A001'}}]);
const first=focused(2.2);focused(4);focused(.1);
assert.deepEqual(first,focused(2.2));
const reset=focused(4)[0],whole=focused(.8)[0];
assert(reset.box.every((n,i)=>Math.abs(n-whole.box[i])<1e-7));
assert.equal(reset.alpha,whole.alpha);
// Successive states can add text without moving or rescaling the same illustration.
const titleCue={kind:'title',at:0,text:'Shared process'};
const pointCue={kind:'point',at:0,text:'Next step'};
for (const [key,cue] of [['reserve_title',titleCue],['reserve_point',pointCue]]) {
  const reserved=setup({image_enter:'none',[key]:true})(0);
  const filled=setup({image_enter:'none',[key]:true},{},[cue])(0);
  const automatic=setup({image_enter:'none'},{},[cue])(0);
  assert.deepEqual(reserved[0].box,filled[0].box,`${key}: adding text must preserve image placement`);
  assert.deepEqual(reserved[0].box,automatic[0].box,`${key}: empty and occupied bands must use the same dimensions`);
  assert(!reserved.some(q=>q.kind==='text'),`${key}: reserving space must not insert placeholder text`);
  assert.deepEqual(setup({[key]:false})(1),frame(1),`${key}=false must preserve legacy layout`);
  assert.deepEqual(setup({[key]:false},{},[cue])(1),setup({},{},[cue])(1),`${key}=false must not suppress actual text space`);
  for (const invalid of [null,0,1,'true','false',[],{}]) {
    assert.throws(()=>setup({[key]:invalid}),new RegExp(`data\\.${key} must be boolean`));
  }
}
const reservedData={image_enter:'none',reserve_title:true,reserve_point:true};
const emptyState=setup(reservedData)(0);
for (const additions of [[titleCue],[pointCue],[titleCue,pointCue]]) {
  assert.deepEqual(emptyState[0].box,setup(reservedData,{},additions)(0)[0].box);
}
assert.notDeepEqual(emptyState[0].box,setup({image_enter:'none'})(0)[0].box,'Both reserved bands must reduce the image region');
console.log('PASS: hidden metadata, opt-ins, image entry and holds, attached annotations, deterministic focus/reset, reserved title/point bands, and legacy layouts.');
