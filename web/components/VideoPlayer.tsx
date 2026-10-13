'use client';
import {useEffect,useRef,useState} from 'react';
import type Hls from 'hls.js';
type Props={src:string,poster?:string,live?:boolean,onTime?:(n:number)=>void,onDuration?:(n:number)=>void,playerRef?:React.MutableRefObject<HTMLVideoElement|null>};
type State='loading'|'ready'|'buffering'|'playing'|'error';
const codeError=(code?:number)=>({1:'Playback dibatalkan.',2:'Media gagal diunduh.',3:'Browser gagal decode video.',4:'Format video tidak didukung.'} as Record<number,string>)[code||0]||'Video tidak dapat dimuat.';
export default function VideoPlayer({src,poster,live,onTime,onDuration,playerRef}:Props){
 const ref=useRef<HTMLVideoElement|null>(null),position=useRef(0);
 const [state,setState]=useState<State>('loading'),[error,setError]=useState(''),[revision,retry]=useState(0);
 useEffect(()=>{
  const video=ref.current;if(!video||!src)return;
  let hls:Hls|undefined,cancelled=false,attempts=0;
  setState('loading');setError('');
  const fail=(reason:string)=>{if(!cancelled){setError(reason);setState('error')}};
  const ready=()=>{if(!video.videoWidth||!video.videoHeight){fail('Media tidak memiliki frame video yang terbaca.');return}
    if(position.current>0&&Number.isFinite(video.duration)){try{video.currentTime=Math.min(position.current,Math.max(0,video.duration-.25))}catch{}}
    setState('ready');onDuration?.(video.duration)};
  const waiting=()=>setState(s=>s==='error'?s:'buffering');
  const onUpdate=()=>{position.current=video.currentTime;onTime?.(video.currentTime)};
  const playing=()=>setState('playing'),mediaError=()=>fail(codeError(video.error?.code));
  video.addEventListener('loadeddata',ready);video.addEventListener('error',mediaError);
  video.addEventListener('waiting',waiting);video.addEventListener('stalled',waiting);
  video.addEventListener('timeupdate',onUpdate);video.addEventListener('playing',playing);
  const isHls=(()=>{try{return new URL(src,location.href).pathname.endsWith('.m3u8')}catch{return src.endsWith('.m3u8')}})();
  if(isHls&&!video.canPlayType('application/vnd.apple.mpegurl')){
   import('hls.js').then(({default:HlsImpl})=>{
    if(cancelled)return;
    if(!HlsImpl.isSupported()){fail('HLS tidak didukung browser ini.');return}
    hls=new HlsImpl({maxBufferLength:30,liveSyncDurationCount:3,enableWorker:true});
    hls.on(HlsImpl.Events.ERROR,(_ev,data)=>{
     if(!data.fatal)return;
     if(attempts++<2){
      if(data.type===HlsImpl.ErrorTypes.NETWORK_ERROR){waiting();hls?.startLoad();return}
      if(data.type===HlsImpl.ErrorTypes.MEDIA_ERROR){waiting();hls?.recoverMediaError();return}
     }
     fail('HLS error: '+data.details+'. Reload player atau gunakan arsip MP4.');hls?.stopLoad();
    });
    hls.attachMedia(video);
    hls.on(HlsImpl.Events.MEDIA_ATTACHED,()=>hls?.loadSource(src));
   }).catch(()=>fail('Player HLS gagal dimuat.'));
  }else{video.src=src;video.load()}
  return()=>{cancelled=true;hls?.destroy();video.removeEventListener('loadeddata',ready);
   video.removeEventListener('error',mediaError);video.removeEventListener('waiting',waiting);
   video.removeEventListener('stalled',waiting);video.removeEventListener('timeupdate',onUpdate);
   video.removeEventListener('playing',playing);video.removeAttribute('src');video.load()};
 },[src,revision]);
 return <div className="video-player-shell">
  <video ref={node=>{ref.current=node;if(playerRef)playerRef.current=node}}
    controls playsInline preload="metadata" poster={poster} className="player" aria-label={live?'Live DVR player':'Clip player'}/>
  {(state==='loading'||state==='buffering')&&<div role="status" className="video-player-status">{state==='loading'?'Menyiapkan video…':'Menunggu buffer video…'}</div>}
  {state==='error'&&<div role="alert" className="video-player-failure"><strong>Video tidak dapat diputar</strong><span>{error}</span>
   <button type="button" onClick={()=>{setState('loading');retry(n=>n+1)}}>Reload player</button></div>}
 </div>;
}