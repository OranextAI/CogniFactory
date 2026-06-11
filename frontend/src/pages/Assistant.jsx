import React, { useState, useRef, useEffect } from 'react';
import {
    Box, TextField, Button, Paper, Typography, CircularProgress,
    Switch, FormControlLabel, Collapse, IconButton, Tooltip, Alert,
    Accordion, AccordionSummary, AccordionDetails
} from '@mui/material';
import SendIcon from '@mui/icons-material/Send';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import FactoryIcon from '@mui/icons-material/Factory';
import { askQuestion, askProduction, testDbConnection } from '../api';

const PRODUCTION_DB_STORAGE_KEY = 'cogni.productionDb.v1';

const defaultDbConfig = {
    host: '4.251.192.31',
    port: '5432',
    user: 'postgres',
    password: '',
    database: 'oranextdb',
};

function loadStoredDb() {
    try {
        const raw = localStorage.getItem(PRODUCTION_DB_STORAGE_KEY);
        if (!raw) return defaultDbConfig;
        const parsed = JSON.parse(raw);
        // Never persist the password — always read it back blank.
        return { ...defaultDbConfig, ...parsed, password: '' };
    } catch (_e) {
        return defaultDbConfig;
    }
}

function persistStoredDb(cfg) {
    try {
        const { password: _omitPassword, ...rest } = cfg;
        localStorage.setItem(PRODUCTION_DB_STORAGE_KEY, JSON.stringify(rest));
    } catch (_e) { /* ignore */ }
}

export default function Assistant() {
    const [messages, setMessages] = useState([
        { role: 'model', parts: [{ text: "Hello! I am FactoryGuard AI. How can I help you with your operations today?" }] }
    ]);
    const [input, setInput] = useState('');
    const [isLoading, setIsLoading] = useState(false);

    // --- Production agent state ---
    const [productionMode, setProductionMode] = useState(false);
    const [dbConfig, setDbConfig] = useState(loadStoredDb);
    const [showPassword, setShowPassword] = useState(false);
    const [testStatus, setTestStatus] = useState(null);  // { ok: bool, message: string }
    const [testing, setTesting] = useState(false);
    const [configOpen, setConfigOpen] = useState(true);

    const messagesEndRef = useRef(null);
    const scrollToBottom = () => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    useEffect(scrollToBottom, [messages]);

    const updateDbField = (field) => (e) => {
        const next = { ...dbConfig, [field]: e.target.value };
        setDbConfig(next);
        persistStoredDb(next);
        setTestStatus(null);
    };

    const handleTestConnection = async () => {
        setTesting(true);
        setTestStatus(null);
        try {
            const { data } = await testDbConnection(dbConfig);
            setTestStatus({
                ok: true,
                message: `Connecté à ${data.database} (${data.table_count} tables) · ${data.version}`,
            });
        } catch (err) {
            const msg = err.response?.data?.error || err.message || 'Échec de connexion';
            setTestStatus({ ok: false, message: msg });
        } finally {
            setTesting(false);
        }
    };

    const handleSend = async () => {
        if (!input.trim() || isLoading) return;

        if (productionMode && !dbConfig.password) {
            setTestStatus({ ok: false, message: 'Mot de passe DB requis en mode Production.' });
            return;
        }

        const userMessage = { role: 'user', parts: [{ text: input }] };
        const newMessages = [...messages, userMessage];
        setMessages(newMessages);
        setInput('');
        setIsLoading(true);

        try {
            if (productionMode) {
                const { data } = await askProduction(input, dbConfig);
                const aiMessage = {
                    role: 'model',
                    parts: [{ text: data.answer || '(réponse vide)' }],
                    sql: data.sql,
                    rows: data.rows,
                    row_count: data.row_count,
                    columns: data.columns,
                };
                setMessages([...newMessages, aiMessage]);
            } else {
                const response = await askQuestion(newMessages);
                const aiMessage = { role: 'model', parts: [{ text: response.data.response }] };
                setMessages([...newMessages, aiMessage]);
            }
        } catch (error) {
            const errorText = error.response?.data?.error
                || (error.response?.data ? JSON.stringify(error.response.data) : null)
                || error.message
                || 'Une erreur réseau est survenue.';
            const errorMessage = { role: 'model', parts: [{ text: `❌ ${errorText}` }] };
            setMessages([...newMessages, errorMessage]);
            console.error('Failed to get response from AI:', error);
        } finally {
            setIsLoading(false);
        }
    };

    const dbFieldsValid = ['host', 'user', 'database'].every((k) => dbConfig[k]?.toString().trim());

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 120px)' }}>
            <Box sx={{ display: 'flex', alignItems: 'center', mb: 1 }}>
                <Typography variant="h4" sx={{ flexGrow: 1 }}>AI Assistant</Typography>
                <FormControlLabel
                    control={
                        <Switch
                            checked={productionMode}
                            onChange={(e) => setProductionMode(e.target.checked)}
                            color="primary"
                        />
                    }
                    label={
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                            <FactoryIcon fontSize="small" />
                            <span>Mode Production</span>
                        </Box>
                    }
                />
            </Box>

            <Collapse in={productionMode} unmountOnExit>
                <Accordion
                    expanded={configOpen}
                    onChange={(_e, v) => setConfigOpen(v)}
                    sx={{ mb: 2 }}
                >
                    <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                        <Typography variant="subtitle1">
                            Connexion à la base Production
                            {testStatus?.ok && ' ✅'}
                        </Typography>
                    </AccordionSummary>
                    <AccordionDetails>
                        <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, display: 'block' }}>
                            L'agent transforme votre question en SQL (SELECT lecture seule), l'exécute sur cette base,
                            puis résume le résultat en français. Le mot de passe n'est jamais sauvegardé en local.
                        </Typography>
                        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '2fr 1fr' }, gap: 1.5, mb: 1.5 }}>
                            <TextField label="Hôte" size="small" value={dbConfig.host} onChange={updateDbField('host')} />
                            <TextField label="Port" size="small" value={dbConfig.port} onChange={updateDbField('port')} />
                        </Box>
                        <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5, mb: 1.5 }}>
                            <TextField label="Utilisateur" size="small" value={dbConfig.user} onChange={updateDbField('user')} />
                            <TextField label="Base" size="small" value={dbConfig.database} onChange={updateDbField('database')} />
                        </Box>
                        <TextField
                            label="Mot de passe"
                            size="small"
                            fullWidth
                            type={showPassword ? 'text' : 'password'}
                            value={dbConfig.password}
                            onChange={updateDbField('password')}
                            sx={{ mb: 1.5 }}
                            InputProps={{
                                endAdornment: (
                                    <Tooltip title={showPassword ? 'Masquer' : 'Afficher'}>
                                        <IconButton size="small" onClick={() => setShowPassword((v) => !v)}>
                                            {showPassword ? <VisibilityOffIcon /> : <VisibilityIcon />}
                                        </IconButton>
                                    </Tooltip>
                                ),
                            }}
                        />
                        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
                            <Button
                                variant="outlined"
                                onClick={handleTestConnection}
                                disabled={testing || !dbFieldsValid || !dbConfig.password}
                            >
                                {testing ? <CircularProgress size={20} /> : 'Tester la connexion'}
                            </Button>
                            {testStatus && (
                                <Alert
                                    severity={testStatus.ok ? 'success' : 'error'}
                                    sx={{ flexGrow: 1, py: 0 }}
                                >
                                    {testStatus.message}
                                </Alert>
                            )}
                        </Box>
                    </AccordionDetails>
                </Accordion>
            </Collapse>

            <Paper elevation={3} sx={{ flexGrow: 1, p: 2, overflowY: 'auto', mb: 2, display: 'flex', flexDirection: 'column' }}>
                {messages.map((msg, index) => (
                    <Box key={index} sx={{ mb: 2, alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start', maxWidth: '85%' }}>
                        <Paper
                            elevation={1}
                            sx={{
                                p: 1.5,
                                backgroundColor: msg.role === 'user' ? 'primary.main' : 'background.default',
                                color: msg.role === 'user' ? 'primary.contrastText' : 'text.primary',
                                borderRadius: msg.role === 'user' ? '20px 20px 5px 20px' : '20px 20px 20px 5px',
                            }}
                        >
                            <Typography sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                {msg.parts[0].text}
                            </Typography>
                        </Paper>
                        {msg.sql && (
                            <Accordion sx={{ mt: 0.5, backgroundColor: 'transparent', boxShadow: 'none' }}>
                                <AccordionSummary expandIcon={<ExpandMoreIcon />} sx={{ minHeight: 0, p: 0 }}>
                                    <Typography variant="caption" color="text.secondary">
                                        Voir le SQL · {msg.row_count} ligne{msg.row_count > 1 ? 's' : ''}
                                    </Typography>
                                </AccordionSummary>
                                <AccordionDetails sx={{ p: 0 }}>
                                    <Box
                                        component="pre"
                                        sx={{
                                            backgroundColor: 'rgba(0,0,0,0.06)',
                                            p: 1.5, borderRadius: 1,
                                            fontFamily: 'monospace', fontSize: '0.75rem',
                                            overflowX: 'auto', whiteSpace: 'pre-wrap',
                                        }}
                                    >
                                        {msg.sql}
                                    </Box>
                                </AccordionDetails>
                            </Accordion>
                        )}
                    </Box>
                ))}
                {isLoading && <CircularProgress size={24} sx={{ alignSelf: 'center', my: 2 }} />}
                <div ref={messagesEndRef} />
            </Paper>

            <Box sx={{ display: 'flex' }}>
                <TextField
                    fullWidth
                    variant="outlined"
                    placeholder={productionMode
                        ? 'Ex: Combien de lignes de production sont actives ?'
                        : 'Ask about safety compliance, sensor data, or production status...'}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && handleSend()}
                    disabled={isLoading}
                />
                <Button variant="contained" onClick={handleSend} sx={{ ml: 1, p: '15px' }} disabled={isLoading}>
                    <SendIcon />
                </Button>
            </Box>
        </Box>
    );
}
