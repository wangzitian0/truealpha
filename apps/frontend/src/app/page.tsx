"use client";

import { useEffect, useState } from "react";
import { counterApi, CountResponse } from "@/lib/api";
import { Play, Plus, RefreshCw, User, Users } from "lucide-react";

export default function Dashboard() {
  const [userId, setUserId] = useState<string>("");
  const [key, setKey] = useState<string>("button.click");
  const [perUserCount, setPerUserCount] = useState<number>(0);
  const [globalCount, setGlobalCount] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Initialize or fetch unique UUID for session
  useEffect(() => {
    let savedId = localStorage.getItem("truealpha_user_id");
    if (!savedId) {
      savedId = crypto.randomUUID();
      localStorage.setItem("truealpha_user_id", savedId);
    }
    setUserId(savedId);
  }, []);

  // Fetch counts
  const fetchCounts = async (currentUserId: string, currentKey: string) => {
    if (!currentUserId || !currentKey) return;
    try {
      setLoading(true);
      setError(null);
      const userRes = await counterApi.getCount(currentKey, currentUserId);
      const globalRes = await counterApi.getCount(currentKey);
      setPerUserCount(userRes.count);
      setGlobalCount(globalRes.count);
    } catch (err: any) {
      setError(err.message || "Failed to fetch counter data");
    } finally {
      setLoading(false);
    }
  };

  // Fetch on mount/change
  useEffect(() => {
    if (userId && key) {
      fetchCounts(userId, key);
    }
  }, [userId, key]);

  // Handle increment
  const handleIncrement = async () => {
    if (!userId || !key) return;
    try {
      setLoading(true);
      setError(null);
      await counterApi.increment(userId, key);
      // Re-fetch to get atomic database counts
      await fetchCounts(userId, key);
    } catch (err: any) {
      setError(err.message || "Failed to increment counter");
      setLoading(false);
    }
  };

  // Generate new user identity
  const handleNewUser = () => {
    const newId = crypto.randomUUID();
    localStorage.setItem("truealpha_user_id", newId);
    setUserId(newId);
  };

  return (
    <div className="space-y-8 animate-fade-in">
      {/* Header Info */}
      <div className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
          DDD Bounded Context Counter
        </h1>
        <p className="text-gray-400 max-w-2xl text-base">
          This dashboard showcases a production-ready, transactional-outbox backed 
          <strong> Counter Bounded Context</strong>. Counts are tracked atomically 
          per-user/key, emitting domain events for other microservices.
        </p>
      </div>

      {/* Main Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        
        {/* User Identity Panel */}
        <div className="md:col-span-1 bg-card border border-border rounded-2xl p-6 flex flex-col justify-between shadow-xl">
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-accent/10 border border-accent/20 flex items-center justify-center text-accent">
                <User size={20} />
              </div>
              <div>
                <h3 className="font-semibold text-white">Active Identity</h3>
                <p className="text-xs text-gray-500">Stored in localStorage</p>
              </div>
            </div>
            
            <div className="space-y-2">
              <label className="text-xs text-gray-400 font-medium block">UUID</label>
              <div className="bg-background border border-border rounded-lg p-3 select-all font-mono text-xs truncate text-gray-300">
                {userId || "Loading..."}
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-xs text-gray-400 font-medium block">Counter Key</label>
              <input
                type="text"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder="domain.action"
                className="w-full bg-background border border-border rounded-lg p-2.5 text-sm text-white focus:outline-none focus:border-accent transition-colors font-mono"
              />
            </div>
          </div>

          <button
            onClick={handleNewUser}
            className="mt-6 w-full py-2.5 px-4 bg-background border border-border rounded-xl text-sm font-medium hover:border-gray-400 hover:text-white transition-all text-gray-300 flex items-center justify-center gap-2"
          >
            <RefreshCw size={14} />
            Switch Active User
          </button>
        </div>

        {/* Counts Panels */}
        <div className="md:col-span-2 grid grid-cols-1 sm:grid-cols-2 gap-6">
          {/* Per-User Count Card */}
          <div className="bg-card border border-border rounded-2xl p-6 flex flex-col justify-between shadow-xl relative overflow-hidden group">
            <div className="absolute top-0 right-0 p-8 opacity-5 text-accent pointer-events-none group-hover:scale-110 transition-transform">
              <User size={120} />
            </div>
            <div className="space-y-2">
              <span className="text-xs font-semibold text-accent tracking-wider uppercase">User Tally</span>
              <h2 className="text-5xl font-extrabold text-white font-mono tracking-tight">
                {perUserCount}
              </h2>
              <p className="text-xs text-gray-400">Total times this specific user triggered the event.</p>
            </div>
            <div className="border-t border-border/60 pt-4 mt-6 flex items-center justify-between text-xs text-gray-500 font-mono">
              <span>FOR USER</span>
              <span className="truncate max-w-[120px]">{userId.split("-")[0]}...</span>
            </div>
          </div>

          {/* Global Count Card */}
          <div className="bg-card border border-border rounded-2xl p-6 flex flex-col justify-between shadow-xl relative overflow-hidden group">
            <div className="absolute top-0 right-0 p-8 opacity-5 text-emerald-400 pointer-events-none group-hover:scale-110 transition-transform">
              <Users size={120} />
            </div>
            <div className="space-y-2">
              <span className="text-xs font-semibold text-emerald-400 tracking-wider uppercase">Global Sum</span>
              <h2 className="text-5xl font-extrabold text-white font-mono tracking-tight">
                {globalCount}
              </h2>
              <p className="text-xs text-gray-400">Combined sum of this event across all users in the system.</p>
            </div>
            <div className="border-t border-border/60 pt-4 mt-6 flex items-center justify-between text-xs text-gray-500 font-mono">
              <span>AGGREGATE</span>
              <span>SUM(users)</span>
            </div>
          </div>
        </div>

      </div>

      {/* Interaction Panel */}
      <div className="bg-gradient-to-r from-card to-card/40 border border-border rounded-2xl p-8 flex flex-col sm:flex-row items-center justify-between gap-6 shadow-xl">
        <div className="space-y-1 text-center sm:text-left">
          <h3 className="text-lg font-bold text-white">Trigger Incremental Event</h3>
          <p className="text-sm text-gray-400">
            Hits the async API boundary to write atomic upserts and event outbox logs.
          </p>
        </div>
        
        <div className="flex items-center gap-4 w-full sm:w-auto">
          <button
            onClick={() => fetchCounts(userId, key)}
            disabled={loading}
            className="flex-1 sm:flex-initial py-3 px-5 bg-background border border-border rounded-xl text-sm font-semibold hover:border-gray-400 hover:text-white transition-all text-gray-300 flex items-center justify-center gap-2 disabled:opacity-50"
          >
            <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          
          <button
            onClick={handleIncrement}
            disabled={loading}
            className="flex-1 sm:flex-initial py-3 px-6 bg-accent hover:bg-accent-hover active:scale-95 text-white rounded-xl text-sm font-semibold shadow-lg shadow-accent/20 transition-all flex items-center justify-center gap-2 disabled:opacity-50"
          >
            <Plus size={18} />
            Increment Count
          </button>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 p-4 rounded-xl text-sm flex items-center gap-3">
          <span className="h-2 w-2 rounded-full bg-red-400 animate-ping" />
          <p className="font-mono text-xs">{error}</p>
        </div>
      )}
    </div>
  );
}
