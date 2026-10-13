'use client';
import { useEffect, useRef } from 'react';
import type Hls from 'hls.js';

type Props={src:string,live?:boolean,onTime?:(time:number)=>void,onDuration?:(duration:number)=>void,playerRef?:React.MutableRefObject<HTMLVideoElement|null>};
export default function VideoPlayer({src,live,onTime,onDuration,playerRef}:Props){
 const localRef=useRef<HTMLVideoElement|null>(null);
 useEffect(()=>{
  const video=localRef.current;
  if(!video||!src)return;
  let hls:Hls|undefined;
  if(src.endsWith('.m3u8')&&!video.canPlayType('application/vnd.apple.mpegurl')){
   let cancelled=false;
   import('hls.js').then(({default:HlsImpl})=>{
    if(cancelled)return;
    if(HlsImpl.isSupported()){
     hls=new HlsImpl({maxBufferLength:30,liveSyncDurationCount:3,enableWorker:true});
     hls.loadSource(src);hls.attachMedia(video);
    }
   });
   return ()=>{cancelled=true;hls?.destroy();video.removeAttribute('src');video.load();};
  }
  video.src=src;
  return ()=>{video.removeAttribute('src');video.load()};
 },[src]);
 return <video ref={el=>{localRef.current=el;if(playerRef)playerRef.current=el}} controls playsInline preload="metadata" className="player" onTimeUpdate={e=>onTime?.(e.currentTarget.currentTime)} onLoadedMetadata={e=>onDuration?.(e.currentTarget.duration)} aria-label={live?'Live DVR player':'Clip player'} />;
}
