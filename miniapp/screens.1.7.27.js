/* Bounded, memory-only screen snapshots. DOM nodes retain values and listeners. */
class CoreScreenCache {
  constructor({owner,now=Date.now,maxEntries=6,maxNodes=12000,maxAge=120000}){
    Object.assign(this,{owner,now,maxEntries,maxNodes,maxAge});this.identity=owner();this.entries=new Map();
  }
  sync(){const identity=this.owner();if(identity!==this.identity){this.clear();this.identity=identity;}}
  clear(){this.entries.clear();}
  put(key,entry){this.sync();const nodes=entry.nodes.reduce((n,x)=>n+1+(x.querySelectorAll?.('*').length||0),0);if(nodes>this.maxNodes)return;this.entries.delete(key);this.entries.set(key,{...entry,nodesCount:nodes,at:this.now()});let total=[...this.entries.values()].reduce((n,x)=>n+x.nodesCount,0);while(this.entries.size>this.maxEntries||total>this.maxNodes){const key=this.entries.keys().next().value;total-=this.entries.get(key).nodesCount;this.entries.delete(key);}}
  take(key){this.sync();const entry=this.entries.get(key);this.entries.delete(key);if(!entry||this.now()-entry.at>this.maxAge)return null;return entry;}
  invalidate(){this.sync();for(const [key,entry] of this.entries){if(entry.dirty)entry.stale=true;else this.entries.delete(key);}}
}
if(typeof module!=='undefined')module.exports={CoreScreenCache};
