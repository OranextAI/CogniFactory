import React, { useRef, useState, useEffect } from 'react';
import { 
    Card, 
    CardContent, 
    Typography, 
    Box, 
    IconButton, 
    Tooltip,
    useTheme,
    Dialog,
    DialogTitle,
    DialogContent,
    DialogActions,
    Button,
    CircularProgress,
    Alert,
    Divider
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import PauseIcon from '@mui/icons-material/Pause';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import VolumeUpIcon from '@mui/icons-material/VolumeUp';
import VolumeOffIcon from '@mui/icons-material/VolumeOff';
import FullscreenIcon from '@mui/icons-material/Fullscreen';
import CameraAltIcon from '@mui/icons-material/CameraAlt';
import axios from 'axios';

export default function VideoPlayer({ title, videoPath, description }) {
    const theme = useTheme();
    const videoRef = useRef(null);
    const canvasRef = useRef(null);
    const [isPlaying, setIsPlaying] = useState(false);
    const [isMuted, setIsMuted] = useState(false);
    const [currentTime, setCurrentTime] = useState(0);
    const [duration, setDuration] = useState(0);
    const [videoError, setVideoError] = useState(null);
    const [videoLoading, setVideoLoading] = useState(true);
    const [analysisDialogOpen, setAnalysisDialogOpen] = useState(false);
    const [analysisLoading, setAnalysisLoading] = useState(false);
    const [analysisResult, setAnalysisResult] = useState('');
    const [analysisError, setAnalysisError] = useState('');

    useEffect(() => {
        console.log('[VideoPlayer] 📹 Composant chargé:', {
            title,
            videoPath,
            mimeType: getMimeType(videoPath),
            timestamp: new Date().toISOString()
        });
    }, [videoPath, title]);

    // Déterminer le type MIME basé sur l'extension
    const getMimeType = (path) => {
        if (!path) return 'video/mp4';
        const ext = path.split('.').pop().toLowerCase().trim();
        const mimeTypes = {
            'mp4': 'video/mp4',
            'webm': 'video/webm',
            'ogg': 'video/ogg',
            'mov': 'video/quicktime'
        };
        const mimeType = mimeTypes[ext] || 'video/mp4';
        console.log(`[VideoPlayer] MIME type: ${mimeType} for extension: ${ext}`);
        return mimeType;
    };

    const handlePlayPause = () => {
        if (videoRef.current) {
            if (isPlaying) {
                videoRef.current.pause();
            } else {
                videoRef.current.play();
            }
            setIsPlaying(!isPlaying);
        }
    };

    const handleRestart = () => {
        if (videoRef.current) {
            videoRef.current.currentTime = 0;
            videoRef.current.play();
            setIsPlaying(true);
        }
    };

    const handleMute = () => {
        if (videoRef.current) {
            videoRef.current.muted = !isMuted;
            setIsMuted(!isMuted);
        }
    };

    const handleFullscreen = () => {
        if (videoRef.current) {
            if (videoRef.current.requestFullscreen) {
                videoRef.current.requestFullscreen();
            } else if (videoRef.current.webkitRequestFullscreen) {
                videoRef.current.webkitRequestFullscreen();
            }
        }
    };

    const handleTimeUpdate = () => {
        if (videoRef.current) {
            setCurrentTime(videoRef.current.currentTime);
        }
    };

    const handleLoadedMetadata = () => {
        if (videoRef.current) {
            setDuration(videoRef.current.duration);
        }
    };

    const handleVideoError = (e) => {
        const video = videoRef.current;
        const errorCode = video?.error?.code;
        const mediaError = video?.error;
        let errorMessage = `Impossible de charger la vidéo`;
        
        console.error('[VideoPlayer] 🎥 ERREUR VIDÉO DÉTAILLÉE:', {
            title: title,
            path: videoPath,
            errorCode: errorCode,
            errorName: {1: 'ABORTED', 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED'}[errorCode],
            mediaError: mediaError,
            fullError: e,
            videoElement: {
                currentSrc: video?.currentSrc,
                networkState: video?.networkState,
                networkStateName: {0: 'NETWORK_EMPTY', 1: 'NETWORK_IDLE', 2: 'NETWORK_LOADING', 3: 'NETWORK_NO_SOURCE'}[video?.networkState],
                readyState: video?.readyState,
                readyStateName: {0: 'HAVE_NOTHING', 1: 'HAVE_METADATA', 2: 'HAVE_CURRENT_DATA', 3: 'HAVE_FUTURE_DATA', 4: 'HAVE_ENOUGH_DATA'}[video?.readyState],
                error: video?.error
            }
        });
        
        if (errorCode === 1) {
            errorMessage = '❌ Chargement annulé (erreur de requête)';
        } else if (errorCode === 2) {
            errorMessage = '❌ Erreur réseau - Vérifiez que le serveur est actif et accessible';
        } else if (errorCode === 3) {
            errorMessage = '❌ Fichier corrompu ou incomplet - Impossible à décoder';
        } else if (errorCode === 4) {
            errorMessage = '❌ Format non supporté - Vérifiez le codec vidéo et le format';
        } else {
            errorMessage = `❌ Erreur inconnue (code: ${errorCode}, state: networkState=${video?.networkState}, readyState=${video?.readyState})`;
        }
        
        setVideoError(errorMessage);
        setVideoLoading(false);
    };

    const handleCanPlay = () => {
        console.log('[VideoPlayer] ✅ Vidéo prête à jouer:', {
            path: videoPath,
            mimeType: getMimeType(videoPath),
            duration: videoRef.current?.duration
        });
        setVideoLoading(false);
        setVideoError(null);
    };

    const formatTime = (time) => {
        if (!time || isNaN(time)) return '0:00';
        const minutes = Math.floor(time / 60);
        const seconds = Math.floor(time % 60);
        return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`;
    };

    const captureVideoFrame = async () => {
        if (!videoRef.current) return null;

        const canvas = canvasRef.current || document.createElement('canvas');
        canvas.width = videoRef.current.videoWidth;
        canvas.height = videoRef.current.videoHeight;

        const context = canvas.getContext('2d');
        context.drawImage(videoRef.current, 0, 0, canvas.width, canvas.height);

        return new Promise((resolve) => {
            canvas.toBlob((blob) => {
                resolve(blob);
            }, 'image/jpeg', 0.9);
        });
    };

    const handleAnalyzeFrame = async () => {
        setAnalysisDialogOpen(true);
        setAnalysisLoading(true);
        setAnalysisError('');
        setAnalysisResult('');

        try {
            // Capture the current frame
            const frameBlob = await captureVideoFrame();

            if (!frameBlob) {
                setAnalysisError('Impossible de capturer une image de la vidéo.');
                setAnalysisLoading(false);
                return;
            }

            // Create FormData for the frame - use new endpoint with Ollama VLM
            const formData = new FormData();
            formData.append('frame_image', frameBlob, 'frame.jpg');
            formData.append('question', 'Analysez cette image de vidéourveillance. Décrivez ce que vous voyez en détail: objets, personnes, conditions éclairage, événements inhabituels, problèmes potentiels.');

            // Send to backend for analysis using Ollama VLM
            // Use the VM IP or localhost depending on environment
            const backendURL = import.meta.env.VITE_API_URL || 'http://localhost:5000';
            const response = await axios.post(`${backendURL}/api/analyze-video-screenshot`, formData, {
                headers: {
                    'Content-Type': 'multipart/form-data'
                },
                timeout: 120000 // 2 minutes timeout for VLM processing
            });

            setAnalysisResult(response.data.analysis);
            setAnalysisLoading(false);
        } catch (error) {
            console.error('Error analyzing frame:', error);
            let errorMessage = 'Erreur lors de l\'analyse du frame vidéo.';
            
            if (error.response?.data?.error) {
                errorMessage = error.response.data.error;
            } else if (error.response?.status === 500) {
                errorMessage = '❌ Erreur serveur 500. Vérifiez que Ollama est en cours d\'exécution sur la VM avec le modèle de vision (qwen2.5vl).';
            } else if (error.response?.status === 400) {
                errorMessage = '❌ Erreur de requête 400. Le frame n\'a pas pu être envoyé.';
            } else if (error.code === 'ERR_NETWORK') {
                errorMessage = '❌ Erreur réseau: impossible de contacter le serveur. Vérifiez que le backend est en cours d\'exécution sur la VM.';
            } else if (error.code === 'ECONNABORTED') {
                errorMessage = '⏱️ Timeout: L\'analyse prend trop de temps. Le modèle VLM sur la VM peut être lent.';
            } else if (error.message) {
                errorMessage = `❌ ${error.message}`;
            }
            
            setAnalysisError(errorMessage);
            setAnalysisLoading(false);
        }
    };

    const handleCloseAnalysisDialog = () => {
        setAnalysisDialogOpen(false);
        setTimeout(() => {
            setAnalysisResult('');
            setAnalysisError('');
        }, 300);
    };

    return (
        <>
            <Card
                sx={{
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    transition: 'all 0.3s ease',
                    background: `linear-gradient(135deg, ${theme.palette.background.paper} 0%, rgba(34, 211, 238, 0.02) 100%)`,
                    border: `1px solid ${theme.palette.divider}`,
                    '&:hover': {
                        boxShadow: '0 8px 24px rgba(34, 211, 238, 0.15)',
                        transform: 'translateY(-4px)'
                    }
                }}
            >
            <CardContent sx={{ flex: 1, display: 'flex', flexDirection: 'column', p: 2 }}>
                <Typography variant="h6" gutterBottom sx={{ fontWeight: 700, mb: 1, display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Box sx={{ width: 4, height: 20, background: `linear-gradient(180deg, ${theme.palette.primary.main} 0%, ${theme.palette.secondary.main} 100%)`, borderRadius: 1 }} />
                    🎬 {title}
                </Typography>

                {videoError && (
                    <Box sx={{ p: 2.5, mb: 2, backgroundColor: 'rgba(239, 68, 68, 0.15)', border: '2px solid #ef4444', borderRadius: 1.5, color: 'error.main' }}>
                        <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>
                            {videoError}
                        </Typography>
                        <Typography variant="caption" sx={{ display: 'block', mt: 1, fontSize: '0.75rem', opacity: 0.8, wordBreak: 'break-all' }}>
                            📍 Chemin: {videoPath}
                        </Typography>
                        <Typography variant="caption" sx={{ display: 'block', mt: 0.5, fontSize: '0.75rem', opacity: 0.8 }}>
                            🔍 Essayez: Ouvrir la console (F12) pour plus de détails
                        </Typography>
                    </Box>
                )}

                {videoLoading && !videoError && (
                    <Box sx={{ p: 2, mb: 2, backgroundColor: 'rgba(59, 130, 246, 0.1)', border: '1px solid #3b82f6', borderRadius: 1, color: 'info.main', display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Box sx={{ animation: 'spin 1s linear infinite', display: 'inline-block' }}>⏳</Box>
                        <Typography variant="caption">Chargement de la vidéo...</Typography>
                    </Box>
                )}

                {description && (
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                        {description}
                    </Typography>
                )}

                {/* Video Player */}
                <Box
                    sx={{
                        position: 'relative',
                        width: '100%',
                        backgroundColor: '#000',
                        borderRadius: 1.5,
                        overflow: 'hidden',
                        mb: 2,
                        flex: 1,
                        minHeight: 250,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        group: '&:hover .video-controls',
                        '&:hover .video-controls': {
                            opacity: 1
                        }
                    }}
                >
                    <video
                        ref={videoRef}
                        style={{
                            width: '100%',
                            height: '100%',
                            objectFit: 'contain',
                        }}
                        onTimeUpdate={handleTimeUpdate}
                        onLoadedMetadata={handleLoadedMetadata}
                        onEnded={() => setIsPlaying(false)}
                        onError={handleVideoError}
                        onCanPlay={handleCanPlay}
                        onStalled={() => console.warn('[VideoPlayer] ⏸️ Video stalled (buffering...)', videoPath)}
                        onSuspend={() => console.warn('[VideoPlayer] ⏸️ Video suspended', videoPath)}
                        onLoadStart={() => {
                            console.log('[VideoPlayer] 🎬 LoadStart triggered for:', videoPath);
                            setVideoLoading(true);
                        }}
                        onPlaying={() => console.log('[VideoPlayer] ▶️ Video playing:', videoPath)}
                        onPause={() => console.log('[VideoPlayer] ⏸️ Video paused')}
                        loop
                        crossOrigin="anonymous"
                    >
                        <source 
                            src={videoPath} 
                            type={getMimeType(videoPath)}
                            onError={() => console.error(`[VideoPlayer] Source error for ${videoPath}`)}
                        />
                        Votre navigateur ne supporte pas le lecteur vidéo HTML5
                    </video>

                    {/* Play Button Overlay */}
                    {!isPlaying && (
                        <Box
                            sx={{
                                position: 'absolute',
                                top: 0,
                                left: 0,
                                width: '100%',
                                height: '100%',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                backgroundColor: 'rgba(0, 0, 0, 0.3)',
                                cursor: 'pointer',
                                transition: 'all 0.3s ease'
                            }}
                            onClick={handlePlayPause}
                        >
                            <Box
                                sx={{
                                    width: 80,
                                    height: 80,
                                    borderRadius: '50%',
                                    background: `linear-gradient(135deg, ${theme.palette.primary.main} 0%, ${theme.palette.secondary.main} 100%)`,
                                    display: 'flex',
                                    alignItems: 'center',
                                    justifyContent: 'center',
                                    boxShadow: '0 8px 24px rgba(167, 139, 250, 0.3)',
                                    '&:hover': {
                                        transform: 'scale(1.1)',
                                    }
                                }}
                            >
                                <PlayArrowIcon sx={{ fontSize: 50, color: 'white' }} />
                            </Box>
                        </Box>
                    )}

                    {/* Video Controls */}
                    <Box
                        className="video-controls"
                        sx={{
                            position: 'absolute',
                            bottom: 0,
                            left: 0,
                            right: 0,
                            backgroundColor: 'rgba(0, 0, 0, 0.7)',
                            p: 1,
                            display: 'flex',
                            alignItems: 'center',
                            gap: 1,
                            opacity: isPlaying ? 0 : 1,
                            transition: 'opacity 0.3s ease'
                        }}
                    >
                        {/* Progress Bar */}
                        <Box
                            sx={{
                                flex: 1,
                                height: 4,
                                backgroundColor: 'rgba(255, 255, 255, 0.2)',
                                borderRadius: 2,
                                cursor: 'pointer',
                                position: 'relative',
                                '&:hover': {
                                    height: 6
                                }
                            }}
                            onClick={(e) => {
                                const rect = e.currentTarget.getBoundingClientRect();
                                const percent = (e.clientX - rect.left) / rect.width;
                                if (videoRef.current) {
                                    videoRef.current.currentTime = percent * duration;
                                }
                            }}
                        >
                            <Box
                                sx={{
                                    height: '100%',
                                    backgroundColor: theme.palette.primary.main,
                                    borderRadius: 2,
                                    width: `${duration ? (currentTime / duration) * 100 : 0}%`,
                                    transition: 'width 0.1s linear'
                                }}
                            />
                        </Box>

                        {/* Time Display */}
                        <Typography variant="caption" sx={{ color: 'white', minWidth: 60 }}>
                            {formatTime(currentTime)} / {formatTime(duration)}
                        </Typography>
                    </Box>
                </Box>

                {/* Controls */}
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                    <Tooltip title={isPlaying ? 'Pause' : 'Lecture'}>
                        <IconButton
                            onClick={handlePlayPause}
                            sx={{
                                color: theme.palette.primary.main,
                                backgroundColor: `rgba(167, 139, 250, 0.1)`,
                                '&:hover': {
                                    backgroundColor: `rgba(167, 139, 250, 0.2)`,
                                }
                            }}
                        >
                            {isPlaying ? <PauseIcon /> : <PlayArrowIcon />}
                        </IconButton>
                    </Tooltip>

                    <Tooltip title="Recommencer">
                        <IconButton
                            onClick={handleRestart}
                            sx={{
                                color: theme.palette.secondary.main,
                                backgroundColor: `rgba(34, 211, 238, 0.1)`,
                                '&:hover': {
                                    backgroundColor: `rgba(34, 211, 238, 0.2)`,
                                }
                            }}
                        >
                            <RestartAltIcon />
                        </IconButton>
                    </Tooltip>

                    <Tooltip title={isMuted ? 'Activer le son' : 'Mute'}>
                        <IconButton
                            onClick={handleMute}
                            sx={{
                                color: theme.palette.success.main,
                                backgroundColor: `rgba(16, 185, 129, 0.1)`,
                                '&:hover': {
                                    backgroundColor: `rgba(16, 185, 129, 0.2)`,
                                }
                            }}
                        >
                            {isMuted ? <VolumeOffIcon /> : <VolumeUpIcon />}
                        </IconButton>
                    </Tooltip>

                    <Tooltip title="Plein écran">
                        <IconButton
                            onClick={handleFullscreen}
                            sx={{
                                color: theme.palette.warning.main,
                                backgroundColor: `rgba(245, 158, 11, 0.1)`,
                                '&:hover': {
                                    backgroundColor: `rgba(245, 158, 11, 0.2)`,
                                }
                            }}
                        >
                            <FullscreenIcon />
                        </IconButton>
                    </Tooltip>

                    <Tooltip title="Analyser avec IA">
                        <IconButton
                            onClick={handleAnalyzeFrame}
                            disabled={analysisLoading}
                            sx={{
                                color: theme.palette.error.main,
                                backgroundColor: `rgba(239, 68, 68, 0.1)`,
                                '&:hover': {
                                    backgroundColor: `rgba(239, 68, 68, 0.2)`,
                                }
                            }}
                        >
                            {analysisLoading ? <CircularProgress size={24} /> : <CameraAltIcon />}
                        </IconButton>
                    </Tooltip>

                    <Typography variant="caption" sx={{ ml: 'auto', color: 'text.secondary', alignSelf: 'center' }}>
                        📁 Vidéo locale
                    </Typography>
                </Box>
            </CardContent>
        </Card>

        {/* Analysis Dialog */}
        <Dialog 
            open={analysisDialogOpen} 
            onClose={handleCloseAnalysisDialog} 
            fullWidth 
            maxWidth="md"
            PaperProps={{
                sx: {
                    background: `linear-gradient(135deg, ${theme.palette.background.paper} 0%, rgba(239, 68, 68, 0.02) 100%)`,
                    borderRadius: 3,
                    border: `1px solid ${theme.palette.divider}`
                }
            }}
        >
            <DialogTitle sx={{ fontWeight: 800, display: 'flex', alignItems: 'center', gap: 1 }}>
                <CameraAltIcon sx={{ color: 'error.main' }} />
                Analyse IA du Frame Vidéo: {title}
            </DialogTitle>
            <Divider />
            <DialogContent sx={{ pt: 3 }}>
                {analysisLoading && (
                    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, py: 4 }}>
                        <CircularProgress />
                        <Typography color="text.secondary">
                            🤖 Ollama (qwen2.5vl) analyse le frame vidéo... Veuillez patienter...
                        </Typography>
                    </Box>
                )}

                {analysisError && (
                    <Alert severity="error" sx={{ mb: 2 }}>
                        {analysisError}
                    </Alert>
                )}

                {analysisResult && (
                    <Box sx={{
                        p: 3,
                        background: `linear-gradient(135deg, rgba(239, 68, 68, 0.05) 0%, rgba(34, 211, 238, 0.05) 100%)`,
                        borderRadius: 2,
                        border: `1px solid ${theme.palette.divider}`,
                        fontFamily: 'system-ui, -apple-system, sans-serif',
                        lineHeight: 1.8,
                        color: theme.palette.text.primary,
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word'
                    }}>
                        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
                            {analysisResult}
                        </Typography>
                    </Box>
                )}
            </DialogContent>
            <DialogActions sx={{ p: 2, gap: 1 }}>
                <Button onClick={handleCloseAnalysisDialog} variant="outlined">
                    Fermer
                </Button>
            </DialogActions>
        </Dialog>
        </>
    );
}
