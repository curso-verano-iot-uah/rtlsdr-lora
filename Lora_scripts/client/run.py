import signal
import sys
import os
import asyncio
from rtlsdr import RtlSdr
import numpy as np
from scipy.signal import welch
from paho_util import *
import time
import threading
import pickle
import socket
from collections import deque

# Configure device
sdr = None
sample_rate = os.getenv("SAMPLE_RATE")
center_freq = float(os.getenv("CENTER_FREQ"))
gain = int(os.getenv("GAIN"))
NFFT = int(os.getenv("NFFT"))
collected_samples = int(os.getenv("COLLECTED_SAMPLES"))
pxx = []
power_result = 0
### MQTT config ###
broker = str(os.getenv("BROKER"))
port = int(os.getenv("PORT"))s
topic = str(os.getenv("TOPIC"))
# generate client ID with pub prefix randomly
client_id = str(os.getenv("CLIENT_ID"))
#username = str(os.getenv("USERNAME"))
#password = str(os.getenv("PASSWORD"))
client = connect_mqtt(client_id=client_id, broker=broker, port=port)
client.loop_start()
### UDP Config ###
udp_host = str(os.getenv("UDP_HOST"))  
udp_port = int(os.getenv("UDP_PORT")) 
udp_buffer = []
len_power_buffer = int(os.getenv("LEN_POWER_BUFFER"))
power_buffer = deque([0] * len_power_buffer)
threshold_count = int(os.getenv("THRESHOLD_COUNT"))
extra_threshold = float(os.getenv("EXTRA_THRESHOLD"))

def handle_sigint(signum, frame):
    global sdr
    print("Received Ctrl+C. Stopping the program and releasing resources...")
    sdr.cancel_read_async()
    sdr.close()
    sys.exit(0)
def sdrConfig(sdr):
    global f
    sdr.sample_rate = sample_rate
    sdr.center_freq = center_freq
    sdr.gain = gain
    f = deque((np.fft.fftfreq(NFFT, 1 / sample_rate) / 1e6)+sdr.center_freq / 1e6)
    f.rotate(128)
async def initial_measurement():
    sdr_initial = RtlSdr()
    sdrConfig(sdr_initial)
    initial_measurement_buffer = []
    initial_collected_samples = collected_samples*10
    total_collected_samples = initial_collected_samples*5
    async for samples in sdr_initial.stream(num_samples_or_bytes=initial_collected_samples):
        initial_measurement_buffer.extend(samples)  # Collect data for the initial measurement
        if len(initial_measurement_buffer) > total_collected_samples:
            break  
        # Calculate the power threshold based on the initial measurement
        _, initial_pxx = welch(x=initial_measurement_buffer, fs=sdr_initial.center_freq, nperseg=NFFT, scaling='spectrum', return_onesided=False)
        pxx = deque(initial_pxx)
        pxx.rotate(128)
        initial_power = np.trapz(pxx, f)  # Integrate in linear scale
        pxx = 10 * np.log10(pxx)
        power_threshold = initial_power
        print(f"Power threshold set to: {power_threshold:.10f}")
    sdr_initial.cancel_read_async()
    sdr_initial.close()
    return power_threshold
async def streaming_task(mqtt_thread_instance, udp_thread_instance):
    global sdr
    buffer = []
    samples_collected = 0
    pwr_threshold = await initial_measurement()
    # Start both threads after the initial measurement
    mqtt_thread_instance.start()
    udp_thread_instance.start()
    pwr_threshold *= (1+extra_threshold)
    sdr = RtlSdr()
    sdrConfig(sdr)
    # Registrar la función de manejo de la señal SIGINT
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        async for samples in sdr.stream(num_samples_or_bytes=collected_samples):
            buffer.extend(samples)
            samples_collected += len(samples)
            if samples_collected >= collected_samples:
                pwr = await process_samples(buffer, sdr)
                buffer = []
                samples_collected = 0
                power_buffer.append(1 if pwr > pwr_threshold else 0)
                if len(power_buffer) > len_power_buffer:
                    power_buffer.popleft()
    finally:
        sys.exit(0)

async def process_samples(buffer,sdr):
    global  udp_buffer
    start_time = time.time()
    _, pxx = welch(x=buffer, fs=sdr.center_freq, nperseg=NFFT, scaling='spectrum', return_onesided=False)
    pxx = deque(pxx)
    pxx.rotate(128)
    power_result = np.trapz(pxx, f)  # Integrate in linear scale
    pxx = 10 * np.log10(pxx)
    end_time = time.time()
    elapsed_time = end_time - start_time
    #print(f"Time taken for data collection and processing: {elapsed_time:.5f} seconds, power={power_result:.10f}")
    process_udp(pxx, power_result)
    return power_result

def process_udp(pxx, power_result):
    data = {
        "frequencies": list(f),
        "pxx": list(pxx),
        "power_result": power_result
    }
    data_bytes = pickle.dumps(data)
    udp_buffer.append(data_bytes)
def mqtt_thread():
    while True:
        print(sum(power_buffer))
        if sum(power_buffer) >= threshold_count:
            publish_mqtt()
        time.sleep(1)
def publish_mqtt(msg="Detected LoRa message"):
    while True:
        time.sleep(0.2)
        result = client.publish(topic, msg)
        # result: [0, 1]
        status = result[0]
        if status == 0:
            print(f"Message `{msg}` sent to topic `{topic}`")
        else:
            print(f"Failed to send message to topic {topic}")
def udp_thread():
    global udp_buffer
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        if udp_buffer:
            data = udp_buffer.pop(0)
            udp_socket.sendto(data, (udp_host, udp_port))

async def main():
    udp_thread_instance = threading.Thread(target=udp_thread)
    mqtt_thread_instance = threading.Thread(target=mqtt_thread)
    mqtt_thread_instance.daemon = True
    udp_thread_instance.daemon = True
    streaming_task_instance = asyncio.create_task(streaming_task(mqtt_thread_instance,udp_thread_instance))    
    await streaming_task_instance    

if __name__ == "__main__":
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
