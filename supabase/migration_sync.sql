-- ============================================================
-- MIGRASI SUPABASE: KOLOM SINKRONISASI DATABASE LOKAL (MINI PC)
-- Jalankan SEKALI di Supabase Dashboard -> SQL Editor -> Run.
-- Aman dijalankan ulang dan tidak menghapus data yang sudah ada.
-- ============================================================

-- 1. Waktu perubahan terakhir kendaraan: menentukan data mana yang lebih baru
--    saat kendaraan diubah di Mini PC dan di Supabase/Netlify pada waktu berdekatan.
ALTER TABLE public.registered_vehicles
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE OR REPLACE FUNCTION public.set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_registered_vehicles_updated_at ON public.registered_vehicles;
CREATE TRIGGER trg_registered_vehicles_updated_at
  BEFORE UPDATE ON public.registered_vehicles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- 2. ID unik log dari Mini PC: mencegah log terkirim dobel saat pengiriman diulang.
ALTER TABLE public.vehicle_logs
  ADD COLUMN IF NOT EXISTS client_uuid UUID UNIQUE;

-- 3. Status perintah buka gerbang yang kedaluwarsa (dikirim saat Mini PC offline)
COMMENT ON COLUMN public.gate_commands.status IS 'PENDING, EXECUTED, EXPIRED';
