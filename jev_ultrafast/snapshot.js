(() => {
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const safe = e => !['file','hidden'].includes(e.type);
  // A password's value never leaves the page: only whether it is filled.
  const secret = e => e.type==='password';
  const val = e => secret(e) ? (e.value ? '[filled]' : '') : e.value;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(document.getElementById(id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (['text','email','url','tel','password'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),val(e),e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),val(e)??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Overflowing scroll containers clip what is below their fold, and are what SCROLL must move.
  // Found from the ancestors of in-viewport content: scanning every element cost ~30% more per snapshot.
  const scrollable=v=>['auto','scroll','overlay'].includes(v);
  const scrollerOf=new Map();
  const scroller=e=>{
    if (!scrollerOf.has(e)) {
      let found=null;
      if (e.scrollHeight>e.clientHeight+2 || e.scrollWidth>e.clientWidth+2) {
        const s=getComputedStyle(e);
        if (scrollable(s.overflowX) || scrollable(s.overflowY)) {
          const b=e.getBoundingClientRect(), left=b.left+e.clientLeft, top=b.top+e.clientTop;
          found={e,s,r:{left,top,right:left+e.clientWidth,bottom:top+e.clientHeight}};
        }
      }
      scrollerOf.set(e,found);
    }
    return scrollerOf.get(e);
  };
  const clipped=(e,r)=>{
    for (let p=e.parentElement; p && p!==document.body; p=p.parentElement) {
      const c=scroller(p);
      if (c && !(r.right>c.r.left && r.left<c.r.right && r.bottom>c.r.top && r.top<c.r.bottom)) return true;
    }
    return false;
  };
  const actions=[];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    // A fixed or absolute descendant can escape its scroller's clip: only drop what the hit test also misses.
    if (clipped(e,{left:x,right:x+1,top:y,bottom:y+1}) && !e.contains(document.elementFromPoint(x,y))) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    const base={node:identity(e),role:rname,label:name(e)||rname,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (secret(e)) base.secret=true;
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(val(e)) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth &&
        !clipped(node,r)) {
      words.push(value); length+=value.length;
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  // Compare meaning and identity. Geometry is always resolved and hit-tested just before input.
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  // A scroller showing no text or control yet (a video above its fold) is found under a pointer grid.
  for (const fx of [.25,.5,.75]) for (const fy of [.25,.5,.75])
    for (let p=document.elementFromPoint(innerWidth*fx,innerHeight*fy); p && p!==document.body; p=p.parentElement)
      scroller(p);
  // The document scrolls unless the viewport is locked (CSS overflow propagation) or a full-viewport backdrop covers it.
  const rootOverflow=getComputedStyle(document.documentElement).overflowY;
  const viewportOverflow=rootOverflow!=='visible' ? rootOverflow : getComputedStyle(document.body).overflowY;
  const backdrop=() => {
    if (document.querySelector('dialog:modal')) return true;
    for (let e=document.elementFromPoint(innerWidth/2,innerHeight/2); e; e=e.parentElement) {
      if (getComputedStyle(e).position!=='fixed') continue;
      const r=e.getBoundingClientRect();
      if (r.left<=0 && r.top<=0 && r.right>=innerWidth && r.bottom>=innerHeight) return true;
    }
    return false;
  };
  const documentFree=!['hidden','clip'].includes(viewportOverflow) && !backdrop();
  // Otherwise move the largest scroller whose visible center is not covered (a modal body, a wide table).
  const shown=r=>({left:Math.max(r.left,0),right:Math.min(r.right,innerWidth),
    top:Math.max(r.top,0),bottom:Math.min(r.bottom,innerHeight)});
  const area=r=>Math.max(0,r.right-r.left)*Math.max(0,r.bottom-r.top);
  const boxes=[...scrollerOf.values()].filter(Boolean).map(c=>({...c,v:shown(c.r)}))
    .filter(c=>area(c.v)>0 && visible(c.e) &&
      c.e.contains(document.elementFromPoint((c.v.left+c.v.right)/2,(c.v.top+c.v.bottom)/2)))
    .sort((a,b)=>area(b.v)-area(a.v));
  const where=e=>{
    const d=e.closest('dialog,[role="dialog"],[role="alertdialog"]');
    if (!d) return ' inside a scrollable area';
    const n=d.hasAttribute('aria-labelledby') || d.hasAttribute('aria-label') ? name(d).slice(0,60) : '';
    return ' inside dialog'+(n ? ' "'+n+'"' : '');
  };
  // shortcut: right-to-left scrollers (negative scrollLeft) get no horizontal scroll; handle if one shows up.
  const SCROLL_STEP=0.8; // of the container's visible size, so no row is skipped between two observations
  const directions={
    down:[1,'y',scrollY+innerHeight<height-2, c=>c.e.scrollTop+c.e.clientHeight<c.e.scrollHeight-2],
    up:[-1,'y',scrollY>0, c=>c.e.scrollTop>0],
    right:[1,'x',false, c=>c.e.scrollLeft+c.e.clientWidth<c.e.scrollWidth-2],
    left:[-1,'x',false, c=>c.e.scrollLeft>0],
  };
  for (const [dir,[sign,axis,documentRoom,room]] of Object.entries(directions)) {
    if (documentFree && documentRoom) {
      actions.push({id:'scroll_'+dir,kind:'scroll',label:'Scroll '+dir,delta:560*sign});
      continue;
    }
    const c=boxes.find(c=>scrollable(axis==='y' ? c.s.overflowY : c.s.overflowX) && room(c));
    if (c) actions.push({id:'scroll_'+dir,kind:'scroll',label:'Scroll '+dir+where(c.e),node:identity(c.e),axis,
      delta:Math.round(sign*SCROLL_STEP*(axis==='y' ? c.v.bottom-c.v.top : c.v.right-c.v.left))});
  }
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll:{y:scrollY,height,boxes:boxes.map(c=>[identity(c.e),c.e.scrollLeft,c.e.scrollTop])},
    actions,marker,page_key,guards,omitted_actions};
})()
