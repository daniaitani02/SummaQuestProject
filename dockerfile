# Use an official Python runtime as a base image
FROM python:3.8

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Download the spaCy language model
RUN python -m spacy download en_core_web_sm

# Make port 5000 available to the world outside this container
EXPOSE 5000


# Run app.py when the container launches
CMD ["python", "app.py"]
