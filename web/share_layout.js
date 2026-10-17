// Sharing card geometry: 1200 wide for landscape videos and 900 for portrait ones. The picture keeps
// the video's shape in even sizes and stays inside the card, as the server requires.
export function shareLayout(width,height) {
    const w=width>=height?1200:900, scale=w/width, footer=w>=1000?148:200;
    let h=Math.round(Math.min(1800,height*scale)/2)*2, artWidth;
    while((artWidth=Math.round(h*width/height/2)*2)>w)h-=2;
    return {width:w,height:h+footer,rect:[Math.floor((w-artWidth)/2),0,artWidth,h],footer};
}
