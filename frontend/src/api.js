import axios from 'axios';

// Backend URL is taken from VITE_API_URL at build time (set in frontend/.env).
// Falls back to localhost for local dev with a Flask backend running on port 5000.
const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:5000';

const apiClient = axios.create({
    baseURL: `${API_URL}/api`,
    headers: { 'Content-Type': 'application/json' },
});

// Dashboard
export const getSensors = () => apiClient.get('/sensors');
export const getStats = () => apiClient.get('/stats');
export const getVideos = () => apiClient.get('/videos');
export const getSensorHistory = (sensorId) => axios.get(`${API_URL}/api/sensor-history/${sensorId}`);

// LLM features
export const askQuestion = (contents) => apiClient.post('/ask', { contents });
export const generateSummary = (sensors) => axios.post(`${API_URL}/api/generate-summary`, { sensors });
export const diagnoseSensor = (sensorId) => axios.post(`${API_URL}/api/diagnose-sensor`, { sensor_id: sensorId });

// Video screenshot analysis using Ollama VLM
export const analyzeVideoScreenshot = async (frameBlob, question = '') => {
    const formData = new FormData();
    formData.append('frame_image', frameBlob, 'frame.jpg');
    formData.append('question', question || 'Analysez cette image de vidéosurveillance. Décrivez ce que vous voyez en détail.');
    return axios.post(`${API_URL}/api/analyze-video-screenshot`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
        timeout: 120000,
    });
};

// Production agent: natural-language question -> SQL on the supplied Postgres DB -> answer
export const askProduction = (question, dbConfig) =>
    axios.post(`${API_URL}/api/ask-production`, { question, db_config: dbConfig }, {
        timeout: 180000,
    });

// Quick credentials check for the production DB config form
export const testDbConnection = (dbConfig) =>
    axios.post(`${API_URL}/api/test-db-connection`, { db_config: dbConfig }, {
        timeout: 15000,
    });
